"""Render v1 beyond the corpus: framing, artifact failures, redaction, the compaction
instruction, golden prefix and tool schemas, and the C7 property across appended turns."""

from collections.abc import Sequence
from pathlib import Path

from hypothesis import given
from hypothesis import strategies as st
from pydantic import JsonValue

from threads.log import Event, Head, Header, ParseError, UnknownEvent, parse_log_line
from threads.log.digest import sha256_hex
from threads.log.jcs import canonicalize
from threads.render import COMPACT_INSTRUCTION, ReadArtifact, esc, render
from threads.result import Err, Ok

GOLDEN = Path(__file__).parent / "golden"
USER: dict[str, JsonValue] = {
    "kind": "user",
    "principal": {"issuer": "api", "tenant": "acme", "subject": "alice"},
}
READ: dict[str, JsonValue] = {
    "name": "read_file",
    "description": "Read a file from the sandbox workspace.",
    "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
    "effect_class": "read_only",
    "output_schema": {"type": "string"},
}
SEARCH: dict[str, JsonValue] = {
    "name": "search_issues",
    "description": "Search the issue tracker.",
    "input_schema": {"type": "object"},
    "effect_class": "idempotent",
    "dedup_window_ms": 60000,
    "defer_loading": True,
}
STARTED: dict[str, JsonValue] = {
    "agent_name": "demo",
    "config_hash": "0" * 64,
    "instructions": "You are a helpful agent.",
    "model": {"provider": "scripted", "name": "scripted-1"},
    "model_params": {"max_tokens": 1024, "temperature": 0.5},
    "adapter": {"name": "scripted", "version": "1", "settings": {}},
    "tools": [READ, SEARCH],
}


def event(seq: int, kind: str, data: dict[str, JsonValue], actor: JsonValue = None) -> Event:
    line: dict[str, JsonValue] = {
        "seq": seq,
        "event_id": f"0192e000-0000-7000-8000-{seq:012d}",
        "thread_id": "0192a000-0000-7000-8000-000000000001",
        "branch_id": "0192b000-0000-7000-8000-000000000001",
        "epoch": 1,
        "type": kind,
        "type_version": 1,
        "time": 1_790_000_000_000 + seq,
        "actor": actor if actor is not None else {"kind": "host"},
        "prev_hash": "0" * 64,
        "critical": True,
        "data": data,
    }
    text = canonicalize(line)
    assert isinstance(text, Ok)
    parsed = parse_log_line(text.value)
    assert isinstance(parsed, Ok), parsed
    value = parsed.value
    assert not isinstance(value, Header | Head | UnknownEvent)
    return value


def user(seq: int, text: str) -> Event:
    return event(seq, "user_input", {"source": "api", "text": text}, USER)


def reply(seq: int, text: str) -> Event:
    data: dict[str, JsonValue] = {
        "request_event_id": f"0192e000-0000-7000-8000-{seq - 1:012d}",
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": None, "output_tokens": None},
        "completeness": "complete",
    }
    return event(seq, "model_response", data, {"kind": "model"})


def store(*blobs: bytes) -> dict[str, bytes]:
    return {sha256_hex(b): b for b in blobs}


def reader(blobs: dict[str, bytes]) -> ReadArtifact:
    def get(sha256: str) -> Ok[bytes] | Err[ParseError]:
        data = blobs.get(sha256)
        return Err(ParseError("artifact_missing", sha256)) if data is None else Ok(data)

    return get


def body(events: Sequence[Event], blobs: dict[str, bytes] | None = None) -> bytes:
    rendered = render(events, reader(blobs or {}))
    assert isinstance(rendered, Ok), rendered
    return rendered.value.body


def ref(data: bytes, media_type: str = "text/plain") -> dict[str, JsonValue]:
    return {"sha256": sha256_hex(data), "bytes": len(data), "media_type": media_type}


def test_esc_escapes_ampersand_first() -> None:
    assert esc("&lt;") == "&amp;lt;"
    assert esc("""<a href="x">'&'</a>""") == (
        "&lt;a href=&quot;x&quot;&gt;&#39;&amp;&#39;&lt;/a&gt;"
    )


def test_golden_prefix_and_tool_schemas() -> None:
    """Line 0 and a tools_changed line, pinned byte for byte: model-invisible spec fields are
    omitted and a deferred spec is a stub until it is loaded."""
    loaded = {k: v for k, v in SEARCH.items() if k != "defer_loading"}
    tools: list[JsonValue] = [READ, loaded]
    tools_hash = canonicalize(tools)
    assert isinstance(tools_hash, Ok)
    changed: dict[str, JsonValue] = {
        "tools": tools,
        "tools_hash": sha256_hex(tools_hash.value.encode()),
    }
    events = [event(1, "thread_started", STARTED), event(2, "tools_changed", changed)]
    rendered = render(events, reader({}))
    assert isinstance(rendered, Ok)
    assert rendered.value.line0 == (GOLDEN / "line0.jsonl").read_bytes()
    assert rendered.value.body == (GOLDEN / "tools_changed.jsonl").read_bytes()


def test_injected_artifact_missing_names_the_carrying_event() -> None:
    memory: dict[str, JsonValue] = {
        "source": "memory",
        "trust": "untrusted_reference",
        "origin": {"id": "mem_1"},
        "ref": ref(b"likes tea"),
    }
    events = [event(1, "thread_started", STARTED), event(2, "injected", memory)]
    assert render(events, reader({})) == Err(
        ParseError("artifact_missing", sha256_hex(b"likes tea"), 2)
    )
    assert b"likes tea" in body(events, store(b"likes tea"))


def test_changed_artifact_is_corrupt_never_substituted() -> None:
    image = b"\x89PNG not really"
    part: dict[str, JsonValue] = {
        "type": "image_ref",
        "ref": ref(image, "image/png"),
        "width": 1,
        "height": 1,
    }
    content: dict[str, JsonValue] = {"source": "api", "content": [part]}
    events = [event(1, "thread_started", STARTED), event(2, "user_input", content, USER)]
    changed = {sha256_hex(image): b"\x89PNG tampered!"}
    result = render(events, reader(changed))
    assert isinstance(result, Err)
    assert (result.error.code, result.error.seq) == ("artifact_corrupt", 2)


def test_redaction_spans_are_utf8_bytes() -> None:
    call: dict[str, JsonValue] = {
        "call_id": "call_1",
        "is_error": False,
        "completeness": "complete",
        "preview": "é secret=hunter2 ok",
        "origin": "executed",
    }
    start = len("é secret=".encode())
    edit: dict[str, JsonValue] = {
        "call_id": "call_1",
        "action": "redact",
        "part": 0,
        "spans": [{"start": start, "end": start + len("hunter2")}],
    }
    events = [
        event(1, "thread_started", STARTED),
        event(2, "tool_result", call, {"kind": "tool"}),
        event(3, "context_edited", {"reason": "guardrail", "edits": [edit]}),
    ]
    last = body(events).splitlines()[-1].decode()
    assert "é secret=[redacted] ok" in last
    assert "hunter2" not in last


def test_compaction_instruction_carries_guides_since_the_last_request() -> None:
    def guide(seq: int, reason: str) -> Event:
        data: dict[str, JsonValue] = {
            "extension": "notes",
            "hook": "before_compact",
            "decision": "guide",
            "reason": reason,
        }
        return event(seq, "hook_decision", data)

    events = [event(1, "thread_started", STARTED), guide(2, "keep <ids>"), guide(3, "and paths")]
    rendered = render(events, reader({}), compaction=True)
    assert isinstance(rendered, Ok)
    last = rendered.value.body.splitlines()[-1]
    instruction = f"{COMPACT_INSTRUCTION}\n\nAdditional instructions:\nkeep <ids>\nand paths"
    expected = canonicalize({"role": "user", "content": [{"type": "text", "text": instruction}]})
    assert expected == Ok(last.decode())


turns = st.lists(st.tuples(st.text(), st.text()), max_size=6)


@given(turns)
def test_render_is_deterministic_and_prefix_stable(texts: list[tuple[str, str]]) -> None:
    """C7 within one epoch: however many turns are appended, line 0 is byte-identical, and
    rendering the same events twice gives the same bytes."""
    events: list[Event] = [event(1, "thread_started", STARTED)]
    for said, answered in texts:
        events += [user(len(events) + 1, said), reply(len(events) + 2, answered)]
    first = render(events[:1], reader({}))
    assert isinstance(first, Ok)
    for end in range(1, len(events) + 1):
        once, twice = render(events[:end], reader({})), render(events[:end], reader({}))
        assert isinstance(once, Ok)
        assert once == twice
        assert once.value.line0 == first.value.line0
        assert once.value.body.startswith(first.value.line0)
