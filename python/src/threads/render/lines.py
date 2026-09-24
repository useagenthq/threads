"""One Render v1 line per model-visible event (spec/schema/README.md, "Render v1" table)."""

from collections.abc import Sequence
from dataclasses import dataclass

from pydantic import JsonValue
from pydantic.experimental.missing_sentinel import MISSING

from threads.log import (
    CompactedEvent,
    CompletedResult,
    Event,
    HeartbeatEvent,
    InjectedEvent,
    MailEnvelope,
    MessageReceivedEvent,
    ModelResponseEvent,
    ModelResponseRecoveredEvent,
    OperatorSender,
    ParseError,
    Span,
    SteerEvent,
    TextBody,
    TextPart,
    ToolResultEvent,
    ToolResultLateEvent,
    ToolsChangedEvent,
    ToolSpec,
    UserInputEvent,
)
from threads.log.jcs import canonicalize
from threads.reduce.handlers import to_json
from threads.reduce.view import RenderView, assistant_parts
from threads.render.artifacts import AnyRef, Part, ReadArtifact, part_refs, read_text
from threads.render.framing import (
    CLEARED,
    REDACTED,
    context,
    esc,
    heartbeat,
    reference,
    user_line,
)
from threads.result import Err, Ok


@dataclass(frozen=True, slots=True)
class Line:
    value: JsonValue
    refs: tuple[AnyRef, ...] = ()
    """Artifacts the line's parts reference: verified before the request is sent."""


def spec_line(spec: ToolSpec) -> JsonValue:
    """A tool as the model sees it: a deferred spec is a stub until tool_search loads it."""
    if spec.defer_loading is True:
        return {"name": spec.name, "description": spec.description, "deferred": True}
    schema: JsonValue = dict(spec.input_schema)
    return {"name": spec.name, "description": spec.description, "input_schema": schema}


def tools_line(tools: Sequence[ToolSpec]) -> JsonValue:
    return [spec_line(t) for t in tools]


def build(view: RenderView, read: ReadArtifact, event: Event) -> Ok[Line | None] | Err[ParseError]:
    """The event's line, or None when it renders nothing. Text held in an artifact is read and
    verified here; the artifacts of media parts are listed in `Line.refs`."""
    if isinstance(event, InjectedEvent):
        return _injected(read, event)
    if isinstance(event, CompactedEvent):
        return _summary(read, event)
    if isinstance(event, MessageReceivedEvent):
        return _mail(read, event)
    return Ok(_plain(view, event))


def _mail(read: ReadArtifact, event: MessageReceivedEvent) -> Ok[Line | None] | Err[ParseError]:
    """A received mail as an untrusted `<message>` user line, from its envelope alone."""
    env = event.data.envelope
    body = _mail_text(read, env, event.seq)
    if isinstance(body, Err):
        return body
    sender = env.from_
    # No member name can produce operator="true".
    who = 'operator="true"' if isinstance(sender, OperatorSender) else f'from="{esc(sender.name)}"'
    ask = f' ask_id="{esc(env.ask_id)}"' if env.kind == "ask" and env.ask_id is not MISSING else ""
    head = f'<message {who} kind="{env.kind}"{ask} untrusted="true">'
    return Ok(Line(user_line(f"{head}\n{esc(body.value)}\n</message>")))


def _mail_text(read: ReadArtifact, env: MailEnvelope, seq: int) -> Ok[str] | Err[ParseError]:
    """A mail's text: its body, a notification's result as RFC 8785 JSON, or a bounce's code."""
    if env.body is not MISSING:
        return _body_text(read, env.body, seq)
    if env.result is MISSING:
        if env.code is MISSING:
            raise AssertionError("the schema requires a bounce's code")
        return Ok(f"bounced: {env.code}")
    value = to_json(env.result)
    if isinstance(env.result, CompletedResult) and isinstance(value, dict):
        output = _body_text(read, env.result.output, seq)
        if isinstance(output, Err):
            return output
        value = {**value, "output": output.value}
    text = canonicalize(value)
    if isinstance(text, Err):
        raise AssertionError(f"a parsed result is always canonical: {text.error}")
    return Ok(text.value)


def _body_text(read: ReadArtifact, body: TextBody, seq: int) -> Ok[str] | Err[ParseError]:
    if body.text is not MISSING:
        return Ok(body.text)
    if body.ref is MISSING:
        raise AssertionError("the schema requires a body's text or ref")
    return read_text(read, body.ref, seq)


def _plain(view: RenderView, event: Event) -> Line | None:
    if isinstance(event, UserInputEvent | SteerEvent):
        return _user(view, event)
    if isinstance(event, HeartbeatEvent):
        return Line(user_line(heartbeat(event.data.running_call_ids)))
    if isinstance(event, ToolsChangedEvent):
        return Line({"role": "tools", "tools": tools_line(event.data.tools)})
    if isinstance(event, ModelResponseEvent | ModelResponseRecoveredEvent):
        return _assistant(view, event)
    if isinstance(event, ToolResultEvent | ToolResultLateEvent):
        return _result(view, event)
    return None


def _user(view: RenderView, event: UserInputEvent | SteerEvent) -> Line | None:
    if event.event_id in view.denied:
        return None
    content = event.data.content
    if content is MISSING:
        return Line(user_line("" if event.data.text is MISSING else event.data.text))
    return _parts_line({"role": "user"}, content)


def _injected(read: ReadArtifact, event: InjectedEvent) -> Ok[Line | None] | Err[ParseError]:
    d = event.data
    if d.text is not MISSING:
        body = d.text
    elif d.ref is not MISSING:
        got = read_text(read, d.ref, event.seq)
        if isinstance(got, Err):
            return got
        body = got.value
    else:
        raise AssertionError("the schema requires one of injected text or ref")
    wrap = reference if d.trust == "untrusted_reference" else context
    return Ok(Line(user_line(wrap(d.source, d.origin.id, body))))


def _summary(read: ReadArtifact, event: CompactedEvent) -> Ok[Line | None] | Err[ParseError]:
    ref = event.data.summary_ref
    got = read_text(read, ref, event.seq)
    if isinstance(got, Err):
        return got
    return Ok(Line(user_line(reference("summary", ref.sha256, got.value))))


def _assistant(
    view: RenderView, event: ModelResponseEvent | ModelResponseRecoveredEvent
) -> Line | None:
    kept = assistant_parts(view, event)
    return _parts_line({"role": "assistant"}, kept) if kept else None


def _result(view: RenderView, event: ToolResultEvent | ToolResultLateEvent) -> Line:
    d = event.data
    head: dict[str, JsonValue] = {"role": "tool", "call_id": d.call_id, "is_error": d.is_error}
    if isinstance(event, ToolResultLateEvent):
        head["late"] = True
    if d.call_id in view.cleared:
        return Line({**head, "content": [{"type": "text", "text": CLEARED.format(d.call_id)}]})
    parts: Sequence[Part] = (
        [TextPart(type="text", text=d.preview)] if d.content is MISSING else d.content
    )
    content: list[JsonValue] = []
    for index, part in enumerate(parts):
        spans = view.redactions.get((d.call_id, index))
        if spans is not None and isinstance(part, TextPart):
            content.append({"type": "text", "text": _redact(part.text, spans)})
        else:
            content.append(to_json(part))
    return Line({**head, "content": content}, part_refs(parts))


def _parts_line(head: dict[str, JsonValue], parts: Sequence[Part]) -> Line:
    content: list[JsonValue] = [to_json(p) for p in parts]
    return Line({**head, "content": content}, part_refs(parts))


def _redact(text: str, spans: Sequence[Span]) -> str:
    # Spans are UTF-8 byte offsets into the original text. Merged into their disjoint union
    # and replaced from the end, every offset stays valid and on a character boundary.
    raw = text.encode("utf-8")
    for start, end in reversed(_union(spans)):
        raw = raw[:start] + REDACTED + raw[end:]
    return raw.decode("utf-8")


def _union(spans: Sequence[Span]) -> list[tuple[int, int]]:
    """Overlapping and adjacent spans merged, in order."""
    merged: list[tuple[int, int]] = []
    for start, end in sorted((s.start, s.end) for s in spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged
