# Spore — Hardware-in-the-Loop (HIL) Test Plan (Phase 0 deliverable)

Prerequisite: all Phase 0 simulation tests green (`pytest spore/tests`).
HIL runs the **same contracts** against real hardware, in escalating risk tiers.
Never skip a tier. Every tier ends with an artifact in `docs/hil_results/`.

## Tier 0 — Pure simulation (CI, unlimited)
- Runner: `spore/sim` + in-memory bus. No broker, no hardware.
- Scope: contract validation, safety validator, state machine, fault matrix F1–F18.
- Exit: 100% of failure-mode tests pass; determinism check (seeded run ×20).

## Tier 1 — Simulator behind a real broker
- Runner: Mosquitto in Docker Compose + `SimulatedDevice` client (Phase 2's
  simulated device client connects over MQTT 5 with per-device token).
- Verify: QoS1 redelivery idempotency (F12), last-will offline detection (F7),
  ack timeouts (F11), telemetry batch ingestion at 1 Hz × 10 virtual devices.
- Exit criteria: backend can prove sent/received/acked/failed/offline for
  every command; zero duplicate stored readings.

## Tier 2 — Bench hardware, chamber empty, loads disconnected from substrate
- Hardware: ESP32/ESPHome or Mycodo mirror board; heater/humidifier replaced by
  low-power lamps/relays driving dummy loads; sensors on the bench.
- Tests:
  - T2.1 Heartbeat & watchdog: kill Wi-Fi for 90 s → actuators OFF ≤ 60 s (F7).
  - T2.2 Command storm: 100 commands/min, random ids → no double actuation.
  - T2.3 Relay stuck-on emulation: drive load manually while controller
    commands OFF → fail-safe within 5 min of sustained divergence (F4/F5).
  - T2.4 OTA edge restart mid-cycle → resume from ledger (F9).
- Safety officer present. Manual E-stop verified before every session.

## Tier 3 — Chamber with inert substrate (no mycelium)
- Full recipe dry-run then live-run over 72 h.
- Verify envelope enforcement end-to-end: attempt out-of-envelope setpoint via
  API (must 422, nothing published), attempt optimizer direct write (must be
  impossible by schema).
- Sensor drift vs reference instrument logged; calibration offsets applied and
  bounded by `max_calibration_offset`.

## Tier 4 — Live biology, single cycle, human on call
- One grow cycle, alerts to phone, manual approval ON for everything.
- Success = complete cycle ledger (start→harvest or contaminate) with no
  unlogged actuation and no envelope breach lasting > 10 min.

## Tier 5 — Unattended soak (Phase 8 gate)
- 14-day unattended run, weekly chaos injection (network cut, sensor spoof,
  process kill). Exit criteria per roadmap: no silent data loss, no unsafe
  actuation, no unrecovered crash, complete ledgers.

## Chaos injections allowed from Tier 2 onward
| Injection | Expected behavior |
|-----------|-------------------|
| Network cut 90 s | Watchdog fail-safe; recovery re-registers; no blind retry |
| Spoof NaN temp | Quality gate quarantines; control stops; alert |
| Kill backend process | Edge safe within watchdog timeout; zero data loss after WAL replay |
| Broker restart | QoS1 + idempotency ⇒ exactly-once effect |
| Clock skew ±5 min | All timestamps UTC; ingest_seq resolves ordering |

## Measurement gates (all tiers)
- Command dispatch → ack p50 < 2 s, p99 < 10 s on local network.
- Telemetry ingestion loss < 0.01% (seq gaps counted automatically).
- Fail-safe entry latency after heartbeat loss ≤ `actuator_watchdog_timeout_seconds`.
