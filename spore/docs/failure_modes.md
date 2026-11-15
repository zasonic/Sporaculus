# Spore — Failure Mode Table (Phase 0)

Detection, automatic response, and the simulation test that proves it.
Every row maps to a test in `spore/tests/test_failure_modes.py`.

| # | Failure mode | Detection | Automatic response | Human involvement | Test |
|---|--------------|-----------|--------------------|-------------------|------|
| F1 | Sensor disconnect (temp NaN / no data) | Quality gate: NaN or age > `sensor_stale_after_seconds` → `stale/quarantined` | Control loop for that metric stops; if `sensor_invalid_behavior=fail_safe` → `enter_fail_safe` | Critical alert, must acknowledge | `test_f1_sensor_disconnect` |
| F2 | Invalid CO2 reading (99999 ppm) | Physically-impossible range check → quarantine; controller ignores quarantined values | Last **valid** control decision held only if within envelope, else fail-safe | Warning alert | `test_f2_invalid_co2` |
| F3 | Humidity sensor saturated (pinned 100%) | Flatline detector + cross-check vs temp/VPD consistency | Humidifier forced off; flag sensor `suspect`; drift report | Review at calibration time | `test_f3_humidity_saturation` |
| F4 | Heater stuck on | Commanded OFF but telemetry temp keeps rising; health reports `fault` state while commanded off | `enter_fail_safe`, relay fault incident, block further heater commands until acknowledged | Critical alert required | `test_f4_heater_stuck_on` |
| F5 | Humidifier stuck on | RH ≥ 99% while commanded off | Fail-safe entry, humidifier disabled | Critical alert | `test_f5_humidifier_stuck_on` |
| F6 | Fan failure | Health `fault` on fan relay; CO2 climbing despite FAE setpoint | Abort recipe, raise CO2 alert, request manual ventilation | Alert | `test_f6_fan_failure` |
| F7 | Device offline (network loss) | Missed heartbeats > `device_offline_after_seconds`; MQTT last-will `offline` | Edge watchdog (independent of backend) forces actuators safe after 60 s | Alert | `test_f7_device_offline_watchdog` |
| F8 | Backend crash mid-cycle | Watchdog on edge; heartbeat stream stops | Same as F7 — safety does not depend on backend liveness | Restart runbook | `test_f8_backend_crash` |
| F9 | Power interruption | Device re-registers with fresh boot timestamp; uptime reset detected | Resume recipe from stage ledger + last validated setpoints inside envelope; else fail-safe | Confirm resume | `test_f9_power_interruption_boot_marker` |
| F10 | Duplicate telemetry burst | `(device,sensor,value)` identical within `duplicate_window_seconds` OR `local_seq` regression | Drop with `quality=duplicate`; never stored twice | none | `test_f10_duplicate_rejection` |
| F11 | Command ack lost (flaky link) | `command_ack_timeout_seconds` elapsed, status SENT/RECEIVED | Mark `TIMED_OUT`; NEVER blind-retry actuating commands; require new idempotency key | Operator decides resend | `test_f11_command_timeout_no_retry` |
| F12 | Duplicate command delivery (QoS1 redelivery) | `command_id` already executed on edge | Edge re-acks `IDEMPOTENT_DUPLICATE` without re-executing | none | `test_f12_idempotent_redelivery` |
| F13 | Stale/expired command replay | Command age > ttl at edge | Ack `STALE_COMMAND`, not executed | none | `test_f13_ttl_expiry` |
| F14 | Optimizer proposes out-of-envelope change | Safety validator `validate_setpoints` / `clamp_recommendation` | Reject outright (commands) or clamp to `max_single_step_delta` (recommendations), audit-logged | Approval always required | `test_f14_optimizer_clamped_and_vetoed` |
| F15 | Out-of-envelope setpoint via API | Gateway validation before MQTT publish | HTTP 422 `SAFETY_ENVELOPE_VIOLATION`, nothing published | none | `test_f15_api_rejects_bad_setpoint` |
| F16 | Illegal stage transition | State machine `can_transition` | HTTP 409 `ILLEGAL_TRANSITION`, ledger untouched | none | `test_f16_illegal_transition` |
| F17 | Vision false-positive contamination | Confidence threshold + human correction workflow | Flag cycle degraded only above threshold AND model confidence; corrections feed active learning | Operator confirms | `test_f17_low_confidence_does_not_abort` |
| F18 | Actuator flapping | Anti-flap deadband + cooldown between recipe switches | Controller suppresses sub-deadband changes | none | `test_f18_deadband_suppression` |

## Non-negotiable invariants

1. **Safety never depends on the backend being alive.** The edge watchdog
   (hardware/timer level) enforces fail-safe when heartbeats stop (F7, F8).
2. **Quarantined telemetry never reaches the optimizer or controller.** (F2)
3. **Actuating commands are never auto-retried after timeout.** (F11)
4. **The optimizer has no code path to relays.** Enforced by schema
   (`OptimizerTrialCreate._no_actuator_keys`) and gateway validation. (F14)
