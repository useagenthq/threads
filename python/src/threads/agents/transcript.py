"""The forwarded transcript of a handoff (spec/schema/README.md, "Handoff scope"): what the user
and the agent said, and the handing-off turn's tool calls and results, which the target gets as
untrusted reference. The tool lines are capped at the source's spill threshold, the oldest
dropped first; the shared vector is spec/conformance/vectors/handoff-transcripts.json."""

from collections.abc import Sequence

from threads.log import (
    Event,
    ModelResponseEvent,
    ModelResponseRecoveredEvent,
    TextPart,
    ToolCallEvent,
    ToolResultEvent,
    ToolResultLateEvent,
    UserInputEvent,
    WokenEvent,
)
from threads.log.jcs import canonicalize
from threads.result import Ok


def _spoken(event: Event) -> str | None:
    if isinstance(event, UserInputEvent):
        return f"user: {event.data.text}" if isinstance(event.data.text, str) else None
    if isinstance(event, ModelResponseEvent | ModelResponseRecoveredEvent):
        said = "".join(p.text for p in event.data.content if isinstance(p, TextPart))
        return f"assistant: {said}" if said else None
    return None


def _tool_line(event: Event) -> str | None:
    if isinstance(event, ToolCallEvent) and event.data.name != "handoff":
        text = canonicalize(dict(event.data.input))
        if not isinstance(text, Ok):
            raise AssertionError("a recorded input always canonicalizes")
        return f"tool_call {event.data.call_id} {event.data.name}: {text.value}"
    if isinstance(event, ToolResultEvent | ToolResultLateEvent):
        flag = " (error)" if event.data.is_error else ""
        return f"tool_result {event.data.call_id}{flag}: {event.data.preview}"
    return None


def handoff_transcript(events: Sequence[Event], cap_bytes: int) -> str:
    opener = max(
        (i for i, e in enumerate(events) if isinstance(e, UserInputEvent | WokenEvent)),
        default=-1,
    )
    lines: list[str] = []
    tools: list[int] = []
    for i, event in enumerate(events):
        said = _spoken(event)
        tool = _tool_line(event) if i > opener else None
        if said is not None:
            lines.append(said)
        elif tool is not None:
            tools.append(len(lines))
            lines.append(tool)
    total = sum(len(lines[i].encode()) for i in tools) + max(len(tools) - 1, 0)
    dropped: list[int] = []
    while tools and total > cap_bytes:
        first = tools.pop(0)
        total -= len(lines[first].encode()) + (1 if tools else 0)
        dropped.append(first)
    if not dropped:
        return "\n".join(lines)
    marker = f"[{len(dropped)} earlier tool lines dropped]"
    gone = set(dropped[1:])
    return "\n".join(
        marker if i == dropped[0] else line for i, line in enumerate(lines) if i not in gone
    )
