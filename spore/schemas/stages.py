"""Biological stage machine (contract-level; engine implementation in Phase 5).

Transitions are exhaustive and asserted by tests. Terminal states:
CONTAMINATED, ABORTED, FLUSH_COMPLETE.
"""
from __future__ import annotations

from enum import Enum


class Stage(str, Enum):
    INOCULATED = "INOCULATED"
    COLONIZING = "COLONIZING"
    COLONIZATION_COMPLETE = "COLONIZATION_COMPLETE"
    PINNING_INITIATED = "PINNING_INITIATED"
    PINNING_ACTIVE = "PINNING_ACTIVE"
    MATURATION = "MATURATION"
    HARVEST_READY = "HARVEST_READY"
    HARVESTED = "HARVESTED"
    FLUSH_COMPLETE = "FLUSH_COMPLETE"
    CONTAMINATED = "CONTAMINATED"
    ABORTED = "ABORTED"


# allowed next stages from each state. Note the multi-flush loop:
# HARVESTED -> PINNING_INITIATED starts flush N+1.
STAGE_TRANSITIONS: dict[Stage, tuple[Stage, ...]] = {
    Stage.INOCULATED: (Stage.COLONIZING, Stage.CONTAMINATED, Stage.ABORTED),
    Stage.COLONIZING: (
        Stage.COLONIZATION_COMPLETE,
        Stage.CONTAMINATED,
        Stage.ABORTED,
    ),
    Stage.COLONIZATION_COMPLETE: (
        Stage.PINNING_INITIATED,
        Stage.CONTAMINATED,
        Stage.ABORTED,
    ),
    Stage.PINNING_INITIATED: (
        Stage.PINNING_ACTIVE,
        Stage.MATURATION,           # fast-track when pins mature quickly
        Stage.CONTAMINATED,
        Stage.ABORTED,
    ),
    Stage.PINNING_ACTIVE: (
        Stage.MATURATION,
        Stage.PINNING_INITIATED,    # pin setback / re-initiation
        Stage.CONTAMINATED,
        Stage.ABORTED,
    ),
    Stage.MATURATION: (
        Stage.HARVEST_READY,
        Stage.CONTAMINATED,
        Stage.ABORTED,
    ),
    Stage.HARVEST_READY: (
        Stage.HARVESTED,
        Stage.CONTAMINATED,
        Stage.ABORTED,
    ),
    Stage.HARVESTED: (
        Stage.PINNING_INITIATED,    # next flush
        Stage.FLUSH_COMPLETE,
        Stage.CONTAMINATED,
        Stage.ABORTED,
    ),
    Stage.FLUSH_COMPLETE: (),       # terminal
    Stage.CONTAMINATED: (),         # terminal
    Stage.ABORTED: (),              # terminal
}

TERMINAL_STAGES = frozenset(s for s, nxt in STAGE_TRANSITIONS.items() if not nxt)


def allowed_transitions(current: Stage | str) -> tuple[Stage, ...]:
    return STAGE_TRANSITIONS[Stage(current)]


def can_transition(current: Stage | str, target: Stage | str) -> bool:
    return Stage(target) in allowed_transitions(current)
