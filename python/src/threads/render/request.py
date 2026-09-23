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
    CompactionRequestedEvent,
    Event,
    EventId,
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
    events: Sequence[Event],
    read: ReadArtifact,
    *,
    compaction: bool = False,
    cause: EventId | None = None,
) -> Ok[Rendered] | Err[ParseError]:
    """The request after `events`, every artifact its parts reference read and verified.
    `compaction` renders the summarizer side request; with `cause`, the one a
    compaction_requested asked for, whose history ends at that request."""
    head = epoch_line0(events)
    if isinstance(head, Err):
        return head
    out = [head.value]
    view = render_view(events)
    request = _requested(events, cause)
    # A requested compaction's history ends at its request, each line rendered as it is now.
    history = events if request is None else [e for e in events if e.seq <= request.seq]
    for event in walk(view, history):
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
        out.append(jsonl(user_line(compact_instruction(events, request))))
    return Ok(Rendered(b"".join(out), head.value))


def _requested(events: Sequence[Event], cause: EventId | None) -> CompactionRequestedEvent | None:
    return next(
        (e for e in events if isinstance(e, CompactionRequestedEvent) and e.event_id == cause),
        None,
    )


def compact_instruction(
    events: Sequence[Event], request: CompactionRequestedEvent | None = None
) -> str:
    """The fixed instruction, plus the before_compact guides since the previous request. For a
    requested compaction: the request's instructions, then the guides appended after it."""
    if request is None:
        since = max(
            (i for i, e in enumerate(events) if isinstance(e, ModelRequestEvent)), default=-1
        )
        extra = _guides(events[since + 1 :])
    else:
        asked = request.data.instructions
        after = [e for e in events if e.seq > request.seq]
        extra = ([] if asked is MISSING else [asked]) + _guides(after)
    return COMPACT_INSTRUCTION + (GUIDE_PREFIX + "\n".join(extra) if extra else "")


def _guides(events: Sequence[Event]) -> list[str]:
    return [
        e.data.reason
        for e in events
        if isinstance(e, HookDecisionEvent)
        and e.data.hook == "before_compact"
        and e.data.decision == "guide"
        and e.data.reason is not MISSING
    ]
