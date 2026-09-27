"""reduce(log): the fold over a resolved chain, its semantic rules and its projections."""

from threadsai.reduce.apply import apply, enter_segment
from threadsai.reduce.fold import Fold
from threadsai.reduce.projections import PROJECTIONS
from threadsai.reduce.state import (
    EffectState,
    ForkPoint,
    HeadRef,
    ReducedState,
    Status,
    reduced_state,
)
from threadsai.reduce.transcript import Role, TranscriptEntry

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
    "apply",
    "enter_segment",
    "reduced_state",
]
