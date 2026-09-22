"""reduce(log): the fold over a resolved chain, its semantic rules and its projections."""

from threads.reduce.apply import apply, enter_segment
from threads.reduce.fold import Fold
from threads.reduce.projections import PROJECTIONS
from threads.reduce.state import (
    EffectState,
    ForkPoint,
    HeadRef,
    ReducedState,
    Status,
    UsageTotals,
    reduced_state,
)
from threads.reduce.transcript import Role, TranscriptEntry

__all__ = [
    "PROJECTIONS",
    "EffectState",
    "Fold",
    "ForkPoint",
    "HeadRef",
    "ReducedState",
    "Role",
    "Status",
    "TranscriptEntry",
    "UsageTotals",
    "apply",
    "enter_segment",
    "reduced_state",
]
