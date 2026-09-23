"""ReducedState: the normative v1 projection both languages compare (spec/conformance/README.md)."""

from dataclasses import dataclass
from typing import Literal

from pydantic import JsonValue

from threads.log import BranchId, CallId, EventId, ParkAddress, ThreadId
from threads.reduce.fold import EffectStatus, Fold
from threads.reduce.transcript import TranscriptEntry, transcript

type Status = Literal["inspection_only", "cancelled", "parked", "in_turn", "idle"]


@dataclass(frozen=True, slots=True)
class HeadRef:
    seq: int
    hash: str
    """SHA-256 of the branch's last line, as stored."""


@dataclass(frozen=True, slots=True)
class EffectState:
    effect_key: str
    call_id: CallId
    status: EffectStatus


@dataclass(frozen=True, slots=True)
class ForkPoint:
    seq: int
    snapshot_event_id: EventId


@dataclass(frozen=True, slots=True)
class UsageTotals:
    input_tokens: int
    output_tokens: int
    unknown_responses: int
    """Responses where either count is null. Unknown is never summed as zero."""


@dataclass(frozen=True, slots=True)
class ReducedState:
    thread_id: ThreadId
    branch_id: BranchId
    epoch: int
    status: Status
    turns_completed: int
    pending_calls: tuple[CallId, ...]
    effects: tuple[EffectState, ...]
    parked: tuple[ParkAddress, ...]
    fork_points: tuple[ForkPoint, ...]
    usage: UsageTotals
    transcript: tuple[TranscriptEntry, ...]
    head: HeadRef

    def to_json(self) -> JsonValue:
        """The wire form the conformance cases compare by deep equality."""
        return {
            "thread_id": self.thread_id,
            "branch_id": self.branch_id,
            "epoch": self.epoch,
            "status": self.status,
            "turns_completed": self.turns_completed,
            "pending_calls": list(self.pending_calls),
            "effects": [
                {"effect_key": e.effect_key, "call_id": e.call_id, "status": e.status}
                for e in self.effects
            ],
            "parked": [{"kind": a.kind, "id": a.id} for a in self.parked],
            "fork_points": [
                {"seq": p.seq, "snapshot_event_id": p.snapshot_event_id} for p in self.fork_points
            ],
            "usage": {
                "input_tokens": self.usage.input_tokens,
                "output_tokens": self.usage.output_tokens,
                "unknown_responses": self.usage.unknown_responses,
            },
            "transcript": [{"role": t.role, "event_id": t.event_id} for t in self.transcript],
            "head": {"seq": self.head.seq, "hash": self.head.hash},
        }


def _status(fold: Fold) -> Status:
    if fold.repair:
        return "inspection_only"
    if fold.cancelled:
        return "cancelled"
    if fold.parked:
        return "parked"
    return "in_turn" if fold.in_turn else "idle"


def usage_totals(fold: Fold) -> UsageTotals:
    return UsageTotals(fold.input_tokens, fold.output_tokens, fold.unknown_responses)


def reduced_state(fold: Fold, head: HeadRef) -> ReducedState:
    """ReducedState of a folded resolved chain whose last line is `head`."""
    if fold.thread_id is None or fold.segment is None:
        raise ValueError("a fold reduces only after its header")
    return ReducedState(
        thread_id=fold.thread_id,
        branch_id=fold.segment,
        epoch=fold.epoch,
        status=_status(fold),
        turns_completed=fold.turns,
        pending_calls=tuple(fold.pending),
        effects=tuple(
            EffectState(key, call_id, status) for key, (call_id, status) in fold.effects.items()
        ),
        parked=tuple(fold.parked),
        fork_points=tuple(ForkPoint(seq, event_id) for seq, event_id in fold.fork_points),
        usage=usage_totals(fold),
        transcript=transcript(fold.events),
        head=head,
    )
