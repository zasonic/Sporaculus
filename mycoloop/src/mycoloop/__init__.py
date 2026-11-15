"""MycoLoop — closed-loop mycology backend (modular monolith).

Module boundaries (Phase 0 contract):
    core      config, db, models, schemas, auth, safety  (shared kernel)
    bus       messaging hardware-abstraction layer (loopback | MQTT 5)
    modules   devices, gateway, ingestion, biology, control, vision, optimizer, audit
    sim       simulated chamber + device client (no physical hardware required)

Hard rule enforced by import structure: the optimizer may only *recommend*.
Actuation always flows control -> gateway -> bus -> device, and the device
owns an independent hardware watchdog.
"""

__version__ = "0.1.0"
