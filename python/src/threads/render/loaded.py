"""Deferred tools' spec artifacts (spec/schema/README.md, "Deferred tools and tool_search"):
read for the tools_loaded line, and the artifact checks of rules 46 and 17 point 6 that import
makes in seq order with the request checks."""

from collections.abc import Mapping, Sequence

from pydantic import ValidationError
from pydantic.experimental.missing_sentinel import MISSING

from threads.log import (
    ArtifactRef,
    Event,
    ParseError,
    ThreadStartedEvent,
    ToolsChangedEvent,
    ToolsLoadedEvent,
    ToolSpec,
)
from threads.log.jcs import canonicalize
from threads.reduce.handlers import to_json
from threads.render.artifacts import ReadArtifact, read_text
from threads.result import Err, Ok

_STUB = ("name", "description", "effect_class", "dedup_window_ms", "ends_turn")


def loaded_spec(read: ReadArtifact, ref: ArtifactRef, seq: int) -> Ok[ToolSpec] | Err[ParseError]:
    """A loaded tool's full spec: its verified artifact parsed as a ToolSpec."""
    text = read_text(read, ref, seq)
    if isinstance(text, Err):
        return text
    try:
        return Ok(ToolSpec.model_validate_json(text.value, strict=True))
    except ValidationError:
        return Err(ParseError("invalid_transition", f"artifact {ref.sha256} is no ToolSpec", seq))


def artifact_error(before: Sequence[Event], event: Event, read: ReadArtifact) -> ParseError | None:
    """Rule 46's artifact checks for a tools_loaded, and point 6's for a tools_changed: the
    loaded spec agrees with its stub and is never deferred again; a full form is the artifact's
    bytes."""
    pins = _pins(before)
    if isinstance(event, ToolsLoadedEvent):
        for tool in event.data.tools:
            error = _loaded_error(read, pins.get(tool.name), tool.spec_ref, event.seq)
            if error is not None:
                return error
    elif isinstance(event, ToolsChangedEvent):
        for spec in event.data.tools:
            pin = pins.get(spec.name)
            if pin is not None and pin.spec_ref is not MISSING and spec.spec_ref is MISSING:
                error = _full_form_error(read, pin.spec_ref, spec, event.seq)
                if error is not None:
                    return error
    return None


def _pins(events: Sequence[Event]) -> Mapping[str, ToolSpec]:
    started = next((e for e in events if isinstance(e, ThreadStartedEvent)), None)
    return {} if started is None else {t.name: t for t in reversed(started.data.tools)}


def _loaded_error(
    read: ReadArtifact, stub: ToolSpec | None, ref: ArtifactRef, seq: int
) -> ParseError | None:
    spec = loaded_spec(read, ref, seq)
    if isinstance(spec, Err):
        return spec.error
    full = spec.value
    deferred = full.defer_loading is not MISSING or full.spec_ref is not MISSING
    agrees = stub is not None and all(getattr(full, k) == getattr(stub, k) for k in _STUB)
    if deferred or not agrees:
        name = full.name
        return ParseError("invalid_transition", f"the spec artifact of {name} widens it", seq)
    return None


def _full_form_error(
    read: ReadArtifact, ref: ArtifactRef, spec: ToolSpec, seq: int
) -> ParseError | None:
    text = read_text(read, ref, seq)
    if isinstance(text, Err):
        return text.error
    mine = canonicalize(to_json(spec))
    if isinstance(mine, Ok) and mine.value == text.value:
        return None
    message = f"the full form of {spec.name} differs from its spec artifact"
    return ParseError("invalid_transition", message, seq)
