"""The two closed lists the eval runner reads (spec/schema/eval.v1.schema.json): which hooks always
leave a hook_decision, and which events user code can append. Both come from the generated
schema types, never a hand-copied list; probe tests keep them honest (tests/evals/test_hook_kinds.py
runs the real loop per hook, tests/evals/test_user_events.py checks every event type and injected
source is classified)."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal, TypeAliasType, get_args

from threads._generated.eval_v1 import (
    ChildThreadSource,
    FrameworkEvent,
    FrameworkSource,
    ObservationHook,
    RecordedHook,
    ScriptableEvent,
    ScriptableSource,
)

type HookKind = Literal["recorded", "observation"]


def _values(alias: TypeAliasType) -> tuple[str, ...]:
    return tuple(str(v) for v in get_args(alias.__value__))


HOOK_KINDS: Final[Mapping[str, HookKind]] = {
    **dict.fromkeys(_values(RecordedHook), "recorded"),
    **dict.fromkeys(_values(ObservationHook), "observation"),
}
"""Every wire hook name's kind: recorded hooks always append a hook_decision, observation hooks
only when they fail or annotate."""


@dataclass(frozen=True, slots=True)
class EventKinds:
    events: tuple[str, ...]
    sources: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class UserEvents:
    """What user code can append, as a closed list (spec lane 22, A.2)."""

    scriptable: EventKinds
    framework: EventKinds
    child_thread: EventKinds


USER_EVENTS: Final = UserEvents(
    EventKinds(_values(ScriptableEvent), _values(ScriptableSource)),
    EventKinds(_values(FrameworkEvent), _values(FrameworkSource)),
    EventKinds((), _values(ChildThreadSource)),
)
