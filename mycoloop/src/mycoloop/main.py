"""App factory: wires the modular monolith, the bus and the optional simulator.

Run:
    uvicorn mycoloop.main:app --reload                 # API only
    MYCOLOOP_SIM_AUTOSTART=1 uvicorn mycoloop.main:app # API + simulated chamber
    MYCOLOOP_BUS=mqtt MYCOLOOP_MQTT_HOST=broker ...    # real MQTT 5
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

import yaml
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse

from . import __version__, db, topics
from .bus import MessageBus, make_bus
from .config import PACKAGE_ROOT, Settings, get_settings
from .modules import audit, biology, control, devices, gateway, ingestion, optimizer, simulation, vision
from .schemas import HealthResponse
from .sim import SimDevice

log = logging.getLogger(__name__)
STATIC_DIR = PACKAGE_ROOT / "static"
CONTRACTS_DIR = PACKAGE_ROOT / "contracts"


def create_app(settings: Settings | None = None, *, bus: MessageBus | None = None) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(level=settings.log_level.upper(),
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db.configure(settings)
        db.init_db(settings)
        app.state.settings = settings
        app.state.bus = bus or make_bus(settings)
        app.state.sim_devices = {}
        gw = gateway.Gateway(app.state.bus, settings)
        gw.start()
        gateway.set_gateway(gw)
        app.state.gateway = gw
        if settings.sim_autostart:
            simulation.spawn(app_state_request(app), settings, settings.sim_device_id)
            log.info("simulated chamber %s attached", settings.sim_device_id)
        try:
            yield
        finally:
            for dev in list(app.state.sim_devices.values()):
                dev.stop()
            gw.stop()
            gateway.set_gateway(None)

    app = FastAPI(
        title="MycoLoop API",
        version=__version__,
        description=(
            "Closed-loop mycology backend. Contract-first modular monolith: "
            "device gateway, validated telemetry, biology ledger, vision features, "
            "and a safety-constrained optimizer that may only recommend.\n\n"
            "**Auth**: `X-API-Key` for operators, `X-Device-Token` for devices."
        ),
        lifespan=lifespan,
    )
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_credentials=False,
                       allow_methods=["*"], allow_headers=["*"])
    # Every Depends(get_settings) resolves to *this* app's settings, so tests and
    # multi-tenant embedding never leak the process-wide cached defaults.
    app.dependency_overrides[get_settings] = lambda: settings

    for module in (devices, gateway, ingestion, biology, control, vision, optimizer, audit):
        app.include_router(module.router)
    if settings.sim_enabled:
        app.include_router(simulation.router)

    @app.get("/healthz", response_model=HealthResponse, tags=["meta"])
    def healthz(request: Request) -> HealthResponse:
        from sqlalchemy import func, select

        from .models import Command, Device
        from .schemas import CommandState, DeviceStatus

        with db.session_scope() as session:
            total = session.scalar(select(func.count(Device.device_id))) or 0
            online = session.scalar(
                select(func.count(Device.device_id)).where(Device.status == DeviceStatus.ONLINE.value)) or 0
            pending = session.scalar(
                select(func.count(Command.command_id)).where(
                    Command.state.in_([CommandState.QUEUED.value, CommandState.SENT.value,
                                       CommandState.RECEIVED.value]))) or 0
        return HealthResponse(
            status="ok", version=__version__, database=db.engine().dialect.name,
            bus=getattr(request.app.state, "bus", None).name if getattr(request.app.state, "bus", None) else "none",
            devices_online=int(online), devices_total=int(total), pending_commands=int(pending),
            dry_run=settings.dry_run,
        )

    @app.get("/console", include_in_schema=False)
    def console() -> FileResponse:
        return FileResponse(STATIC_DIR / "console.html")

    @app.get("/", include_in_schema=False)
    def root() -> JSONResponse:
        return JSONResponse({
            "name": "MycoLoop", "version": __version__,
            "console": "/console", "docs": "/docs", "openapi": "/openapi.json",
            "contracts": ["/contracts/asyncapi.yaml", "/contracts/safety-envelope.yaml", "/contracts/schemas.json"],
        })

    @app.get("/contracts/asyncapi.yaml", include_in_schema=False)
    def asyncapi() -> PlainTextResponse:
        return PlainTextResponse((CONTRACTS_DIR / "asyncapi.yaml").read_text(), media_type="application/yaml")

    @app.get("/contracts/safety-envelope.yaml", include_in_schema=False)
    def envelope() -> PlainTextResponse:
        return PlainTextResponse((CONTRACTS_DIR / "safety-envelope.yaml").read_text(), media_type="application/yaml")

    @app.get("/contracts/topics.json", include_in_schema=False)
    def topic_contract() -> JSONResponse:
        return JSONResponse({
            "status": topics.STATUS, "telemetry": topics.TELEMETRY, "commands": topics.COMMANDS,
            "command_ack": topics.COMMAND_ACK, "health": topics.HEALTH, "backend_status": topics.BACKEND_STATUS,
        })

    @app.get("/contracts/schemas.json", include_in_schema=False)
    def schema_contract() -> JSONResponse:
        from . import schemas as s

        names = ["Telemetry", "TelemetryBatch", "CommandCreate", "CommandAck", "CommandRead", "DeviceHealth",
                 "DeviceStatusMessage", "Setpoints", "SafetyEnvelope", "GrowCycleCreate", "StageTransition",
                 "HarvestCreate", "ContaminationCreate", "PhenotypeFeatures", "VisionFrameCreate",
                 "OptimizerTrialCreate", "RecommendationRead", "SearchSpace"]
        return JSONResponse({n: getattr(s, n).model_json_schema() for n in names})

    return app


def app_state_request(app: FastAPI):
    """Tiny shim so lifespan code can reuse the simulation spawn helper."""
    class _R:
        pass

    r = _R()
    r.app = app
    return r


app = create_app()


def run() -> None:  # pragma: no cover
    import uvicorn

    settings = get_settings()
    uvicorn.run("mycoloop.main:app", host="0.0.0.0", port=8000, log_level=settings.log_level.lower())


if __name__ == "__main__":  # pragma: no cover
    run()
