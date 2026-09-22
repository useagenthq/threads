"""Render v1: the one normative byte form of a model request (spec/schema/README.md).

Line 0 is the declared prefix of the request's settings epoch; then one line per model-visible
event in log order; then, for a compaction side request, the fixed instruction. Each line is
RFC 8785 JSON followed by `\\n`.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from pydantic import JsonValue
from pydantic.experimental.missing_sentinel import MISSING

from threads.log import (
    Event,
    HookDecisionEvent,
    ModelRequestEvent,
    ModelSettings,
    ParseError,
    SettingsChangedEvent,
    ThreadStartedData,
    ThreadStartedEvent,
)
from threads.log.jcs import canonicalize
from threads.reduce.handlers import to_json
from threads.reduce.view import render_view, walk
from threads.render.artifacts import ReadArtifact, read_verified
from threads.render.framing import COMPACT_INSTRUCTION, GUIDE_PREFIX, user_line
from threads.render.lines import build, tools_line
from threads.result import Err, Ok


@dataclass(frozen=True, slots=True)
class Rendered:
    body: bytes
    """The whole request: what `request_ref` holds and `req_hash` covers."""
    line0: bytes
    """The declared prefix, with its newline."""


def jsonl(value: JsonValue) -> bytes:
    match canonicalize(value):
        case Ok(value=text):
            return text.encode("utf-8") + b"\n"
        case Err(error=reason):
            # Every rendered value comes from parsed, admitted lines, so it always canonicalizes.
            raise AssertionError(reason)


def line0(started: ThreadStartedData, settings: ModelSettings | None) -> bytes:
    """The declared prefix: system and tools from thread_started, the rest from the epoch."""
    epoch = started if settings is None else settings
    head: dict[str, JsonValue] = {
        "adapter": to_json(epoch.adapter),
        "model": to_json(epoch.model),
        "params": dict(epoch.model_params),
        "system": started.instructions,
        "tools": tools_line(started.tools),
    }
    return jsonl(head)


def epoch_line0(events: Sequence[Event]) -> Ok[bytes] | Err[ParseError]:
    """Line 0 of the settings epoch the next request after `events` belongs to."""
    started: ThreadStartedData | None = None
    settings: ModelSettings | None = None
    for event in events:
        if isinstance(event, ThreadStartedEvent):
            started, settings = event.data, None
        elif isinstance(event, SettingsChangedEvent):
            settings = event.data.settings
    if started is None:
        seq = events[-1].seq + 1 if events else 1
        return Err(ParseError("invalid_transition", "no thread_started before the request", seq))
    return Ok(line0(started, settings))


def render(
    events: Sequence[Event], read: ReadArtifact, *, compaction: bool = False
) -> Ok[Rendered] | Err[ParseError]:
    """The request after `events`, every artifact its parts reference read and verified.
    `compaction` renders the summarizer side request."""
    head = epoch_line0(events)
    if isinstance(head, Err):
        return head
    out = [head.value]
    view = render_view(events)
    for event in walk(view, events):
        built = build(view, read, event)
        if isinstance(built, Err):
            return built
        line = built.value
        if line is None:
            continue
        for ref in line.refs:
            verified = read_verified(read, ref, event.seq)
            if isinstance(verified, Err):
                return verified
        out.append(jsonl(line.value))
    if compaction:
        out.append(jsonl(user_line(compact_instruction(events))))
    return Ok(Rendered(b"".join(out), head.value))


def compact_instruction(events: Sequence[Event]) -> str:
    """The fixed instruction, plus the before_compact guides since the previous request."""
    guides: list[str] = []
    for event in reversed(events):
        if isinstance(event, ModelRequestEvent):
            break
        if isinstance(event, HookDecisionEvent):
            d = event.data
            if d.hook == "before_compact" and d.decision == "guide" and d.reason is not MISSING:
                guides.append(d.reason)
    if not guides:
        return COMPACT_INSTRUCTION
    return COMPACT_INSTRUCTION + GUIDE_PREFIX + "\n".join(reversed(guides))
