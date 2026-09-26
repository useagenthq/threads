"""The `simulate` option of `save_case` (spec lane 32, A): the case.json field, and the prefix
effects a live simulation re-drives against. The prefix's stubs share the saved turn's key space
and are marked `scope: "prefix"`, so an offline rerun ignores them."""

from collections.abc import Sequence
from typing import Annotated, Literal, NotRequired, TypedDict

from pydantic import Field, JsonValue, TypeAdapter, ValidationError

from threads._generated.eval_v1 import CaseSimulate, SimulateBlocked
from threads.log import (
    EffectBeginEvent,
    EffectCommitEvent,
    EffectResolvedEvent,
    Event,
    ParseError,
    ToolCallEvent,
)
from threads.result import Err, Ok
from threads.thread.case_files import ReadArtifact, stubs_of


class SimulateModel(TypedDict):
    """A model plays the user, from a persona and a goal the author writes."""

    kind: Literal["model"]
    persona: str
    goal: str
    max_messages: NotRequired[int]


class SimulateScript(TypedDict):
    """Fixed user messages, sent in order after the opener. No user model is needed."""

    kind: Literal["script"]
    messages: Sequence[str]


type Simulate = SimulateModel | SimulateScript
"""spec/api.json `Simulate`: a plain dict, as lane 16 accepted for option unions."""

_SIMULATE: TypeAdapter[CaseSimulate] = TypeAdapter(
    Annotated[CaseSimulate, Field(discriminator="kind")]
)
_TAGS = frozenset({"model", "script"})


def _field(loc: tuple[int | str, ...]) -> str:
    """The option the author got wrong: the union's tag and wrapper names aren't fields."""
    parts = [str(p) for p in loc if str(p) not in _TAGS and not str(p).startswith("CaseSimulate")]
    return ".".join(parts) or "kind"


def simulate_field(simulate: Simulate) -> Ok[CaseSimulate] | Err[ParseError]:
    """The case.json field, checked against the schema: a bad field is invalid_request, by name."""
    try:
        return Ok(_SIMULATE.validate_python(dict(simulate), strict=True))
    except ValidationError as invalid:
        first = invalid.errors()[0]
        return Err(
            ParseError("invalid_request", f"simulate.{_field(first['loc'])}: {first['msg']}")
        )


def _settled(log: Sequence[Event]) -> set[str]:
    return {e.data.call_id for e in log if isinstance(e, EffectCommitEvent | EffectResolvedEvent)}


def _unsettled(prefix: Sequence[Event], log: Sequence[Event]) -> set[str]:
    """The prefix's effectful calls nothing ever settled: they have no result to stub."""
    done = _settled(log)
    return {
        e.data.call_id
        for e in prefix
        if isinstance(e, EffectBeginEvent) and e.data.call_id not in done
    }


async def prefix_stubs(
    prefix: Sequence[Event],
    turn: Sequence[JsonValue],
    log: Sequence[Event],
    read: ReadArtifact,
) -> Ok[tuple[list[JsonValue], SimulateBlocked | None]] | Err[ParseError]:
    """stubs.json for a simulated case: the prefix's settled effects, then the saved turn's,
    numbered as one queue per (tool, args_hash) so a live run consumes them in log order. An
    unsettled prefix effect blocks the simulation instead, and the case stays runnable offline."""
    open_calls = _unsettled(prefix, log)
    kept = [
        e for e in prefix if not (isinstance(e, ToolCallEvent) and e.data.call_id in open_calls)
    ]
    built = await stubs_of(kept, read)
    if isinstance(built, Err):
        return built
    earlier = [{**s, "scope": "prefix"} for s in built.value if isinstance(s, dict)]
    blocked: SimulateBlocked | None = "unsettled_effect" if open_calls else None
    return Ok((numbered([*earlier, *turn]), blocked))


def numbered(stubs: Sequence[JsonValue]) -> list[JsonValue]:
    """occurrence counts earlier entries with the same (tool, args_hash), in log order."""
    seen: dict[tuple[str, str], int] = {}
    out: list[JsonValue] = []
    for stub in stubs:
        if not isinstance(stub, dict):
            raise AssertionError("a stub entry is an object")
        key = (str(stub["tool"]), str(stub["args_hash"]))
        occurrence = seen.get(key, 0)
        seen[key] = occurrence + 1
        out.append({**stub, "occurrence": occurrence})
    return out
