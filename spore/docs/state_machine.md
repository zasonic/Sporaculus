# Spore — Grow Stage State Machine (Phase 0 contract)

Implemented in code at `spore/schemas/stages.py` (authoritative); this is the
human-readable diagram. Phase 5 builds the transition engine on top of it.

```text
                                  ┌──────────────┐
                                  │  INOCULATED  │
                                  └──────┬───────┘
                                         ▼
                                  ┌──────────────┐
                         ┌───────►│  COLONIZING  │
                         │        └──────┬───────┘  (colonization_percent from vision)
                         │               ▼
                         │    ┌─────────────────────────┐
                         │    │ COLONIZATION_COMPLETE   │
                         │    └────────────┬────────────┘
                 (re-colonize /        │
                  flush prep)          ▼
                         │    ┌────────────────────┐◄──────┐
                         │    │ PINNING_INITIATED  │       │ next flush
                         │    └────────────┬───────┘       │
                         │                 ▼               │
                         │    ┌────────────────────┐       │
                         │    │  PINNING_ACTIVE    ├───────┤ (pin setback)
                         │    └────────────┬───────┘               │
                         │                 ▼                       │
                         │    ┌────────────────────┐               │
                         │    │     MATURATION     │               │
                         │    └────────────┬───────┘               │
                         │                 ▼                       │
                         │    ┌────────────────────┐               │
                         │    │   HARVEST_READY    │               │
                         │    └────────────┬───────┘               │
                         │                 ▼                       │
                         │    ┌────────────────────┐               │
                         │    │     HARVESTED      ├───────────────┘
                         │    └────────────┬───────┘
                         │                 ▼
                         │    ┌────────────────────┐
                         │    │   FLUSH_COMPLETE   │ (terminal)
                         │    └────────────────────┘

  ANY non-terminal state ──► CONTAMINATED (terminal)   [vision model + human confirm]
  ANY non-terminal state ──► ABORTED      (terminal)   [operator decision]
```

## Rules

- Transitions are recorded **only** through the append-only ledger
  (`StageEvent`, monotonic `event_id` per cycle). No direct stage writes.
- `stage_transition` events with an illegal target return HTTP 409
  `ILLEGAL_TRANSITION` and are never persisted.
- Vision pipeline may *propose* transitions (actor=`vision-pipeline`) but
  CONTAMINATED additionally requires either confidence ≥ threshold plus human
  confirmation, per policy (see failure mode F17).
- Minimum stage duration (`anti_flapping.min_stage_duration_minutes = 30`)
  blocks rapid oscillation between stages.

## Terminal states

`CONTAMINATED`, `ABORTED`, `FLUSH_COMPLETE` — no outgoing transitions; a new
cycle starts with a fresh `GrowCycle` record referencing the same strain or a
new one.
