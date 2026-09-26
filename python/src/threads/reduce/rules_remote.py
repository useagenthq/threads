"""Semantic rules 56, 57 and 58 (spec/schema/README.md): A2A calls. A remote_call is directly
followed by the effect_begin of its call, it appears once per call_id, and a remote_task_state is
observed only for a call whose effect committed."""

from collections.abc import Mapping

from threads.log import (
    CallId,
    EffectBeginEvent,
    Event,
    ParseError,
    RemoteCallEvent,
    RemoteTaskStateEvent,
    UnknownEvent,
)
from threads.reduce.fold import Fold, reject
from threads.reduce.handlers import Handler, on


def follows_call(fold: Fold, event: Event | UnknownEvent) -> ParseError | None:
    """Rule 56: nothing comes between a remote_call and the effect_begin of its call, whatever
    this event is, known or not."""
    call_id = fold.remote_begin
    if call_id is None or (isinstance(event, EffectBeginEvent) and event.data.call_id == call_id):
        return None
    return reject(event, f"remote_call {call_id} is not followed by its effect_begin")


def advance(fold: Fold, event: Event) -> None:
    """The begin the remote_call waited for closes its slot (rule 56)."""
    if isinstance(event, EffectBeginEvent):
        fold.remote_begin = None


def _committed(fold: Fold, call_id: CallId) -> bool:
    """Whether the call's effect has an effect_commit: the receipt rule 57 asks for."""
    call = fold.calls.get(call_id)
    if call is None:
        return False
    effect = fold.effects.get(f"{call.branch_id}:{call_id}")
    return effect is not None and effect[1] == "committed"


def _remote_call(fold: Fold, event: RemoteCallEvent) -> ParseError | None:
    """Rule 58: one remote_call per call_id, so every attempt sends the bytes it stored."""
    call_id = event.data.call_id
    if call_id in fold.remote_calls:
        return reject(event, f"call_id {call_id} already has a remote_call")
    fold.remote_calls.add(call_id)
    fold.remote_begin = call_id
    return None


def _task_state(fold: Fold, event: RemoteTaskStateEvent) -> ParseError | None:
    """Rule 57: a partner's task state is observed only for a call we hold a receipt for."""
    call_id = event.data.call_id
    if call_id not in fold.remote_calls:
        return reject(event, f"remote_task_state for {call_id}, which has no remote_call")
    if not _committed(fold, call_id):
        return reject(event, f"remote_task_state for {call_id}, whose effect has no commit")
    return None


HANDLERS: Mapping[type, Handler] = dict(
    [on(RemoteCallEvent, _remote_call), on(RemoteTaskStateEvent, _task_state)]
)
