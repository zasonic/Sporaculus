"""Database wiring: one engine, short-lived sessions, dialect-aware bootstrap.

SQLite (dev/test, zero services) and PostgreSQL 16 + TimescaleDB (production)
share the same models. Timescale specifics are isolated here and in the Alembic
migration so nothing else in the codebase knows which database it is talking to.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool, StaticPool

from .config import Settings, get_settings
from .models import APPEND_ONLY_TABLES, Base

log = logging.getLogger(__name__)

_engine: Engine | None = None
_Session: sessionmaker[Session] | None = None


def _build_engine(settings: Settings) -> Engine:
    url = settings.database_url
    kwargs: dict = {"future": True, "pool_pre_ping": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 15}
        kwargs["poolclass"] = StaticPool if ":memory:" in url else NullPool
    eng = create_engine(url, **kwargs)
    if url.startswith("sqlite"):
        @event.listens_for(eng, "connect")
        def _pragmas(dbapi_conn, _rec):  # pragma: no cover - driver hook
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=8000")
            cur.close()
    return eng


def configure(settings: Settings | None = None) -> Engine:
    """(Re)bind the module-level engine. Called by the app factory and tests."""
    global _engine, _Session
    settings = settings or get_settings()
    if _engine is not None:
        _engine.dispose()
    _engine = _build_engine(settings)
    _Session = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def engine() -> Engine:
    if _engine is None:
        configure()
    assert _engine is not None
    return _engine


def sessions() -> sessionmaker[Session]:
    if _Session is None:
        configure()
    assert _Session is not None
    return _Session


@contextmanager
def session_scope() -> Iterator[Session]:
    """For background workers / bus handlers: commit or roll back, always close."""
    s = sessions()()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def get_session() -> Iterator[Session]:
    """FastAPI dependency."""
    with session_scope() as s:
        yield s


def install_append_only_guards(eng: Engine) -> None:
    """Enforce the append-only ledger at the database level, not just in code."""
    dialect = eng.dialect.name
    with eng.begin() as conn:
        for table in APPEND_ONLY_TABLES:
            if dialect == "sqlite":
                for op in ("UPDATE", "DELETE"):
                    conn.execute(text(
                        f'CREATE TRIGGER IF NOT EXISTS {table}_no_{op.lower()} '
                        f'BEFORE {op} ON {table} BEGIN '
                        f"SELECT RAISE(ABORT, '{table} is append-only'); END;"
                    ))
            elif dialect == "postgresql":
                conn.execute(text(
                    "CREATE OR REPLACE FUNCTION mycoloop_append_only() RETURNS trigger AS $$ "
                    "BEGIN RAISE EXCEPTION '% is append-only', TG_TABLE_NAME; END; $$ LANGUAGE plpgsql;"
                ))
                conn.execute(text(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table};"))
                conn.execute(text(
                    f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
                    "FOR EACH ROW EXECUTE FUNCTION mycoloop_append_only();"
                ))


def enable_timescale(eng: Engine) -> bool:
    """Turn telemetry_clean into a hypertable + continuous aggregate. No-op on SQLite."""
    if eng.dialect.name != "postgresql":
        return False
    try:
        with eng.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS timescaledb"))
            # Timescale requires the partition column inside every unique index.
            conn.execute(text("ALTER TABLE telemetry_clean DROP CONSTRAINT IF EXISTS telemetry_clean_pkey"))
            conn.execute(text("ALTER TABLE telemetry_clean ADD PRIMARY KEY (id, ts)"))
            conn.execute(text(
                "SELECT create_hypertable('telemetry_clean', 'ts', "
                "if_not_exists => TRUE, migrate_data => TRUE)"
            ))
            conn.execute(text(
                "CREATE MATERIALIZED VIEW IF NOT EXISTS telemetry_5min "
                "WITH (timescaledb.continuous) AS "
                "SELECT device_id, time_bucket('5 minutes', ts) AS bucket, "
                "avg(temperature_c) AS avg_temp_c, avg(humidity_pct) AS avg_humidity_pct, "
                "avg(co2_ppm) AS avg_co2_ppm, count(*) AS samples "
                "FROM telemetry_clean GROUP BY device_id, bucket WITH NO DATA"
            ))
        log.info("TimescaleDB hypertable + continuous aggregate ready")
        return True
    except Exception as exc:  # pragma: no cover - requires a live PG+TS
        log.warning("Timescale setup skipped: %s", exc)
        return False


def init_db(settings: Settings | None = None) -> Engine:
    settings = settings or get_settings()
    eng = engine()
    if settings.create_schema_on_boot:
        Base.metadata.create_all(eng)
    install_append_only_guards(eng)
    if settings.timescale_enabled:
        enable_timescale(eng)
    return eng


def reset_for_tests() -> None:
    global _engine, _Session
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _Session = None
