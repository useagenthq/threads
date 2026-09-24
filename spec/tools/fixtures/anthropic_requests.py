# pyright: strict
"""The Anthropic request vector (spec/conformance/vectors/anthropic-requests.json;
spec/schema/README.md, "Cache controls"): Render v1 bytes and the Messages API body both adapters
must build from them, compared as RFC 8785 canonical JSON. Each base case is written out by hand
with caching off; the cached variants apply the placement rule below, written from the lane 10
spec, never read from an implementation."""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING

from .common import CASES, aref, sha
from .jcs import JsonValue, canonical
from .pieces import dump

if TYPE_CHECKING:
    from .jcs import Obj

VECTOR = CASES.parent / "vectors" / "anthropic-requests.json"
MODEL: Obj = {"provider": "anthropic", "name": "claude-sonnet-5"}
PARAMS: Obj = {"max_tokens": 1024}
DOC = b"The grass is green. The sky is blue."
DOC_REF = aref(DOC, "text/plain")
TOOL: Obj = {
    "name": "read_file",
    "description": "Read a file.",
    "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
}
TTLS: tuple[str | None, ...] = (None, "5m", "1h")


def _text(t: str) -> Obj:
    return {"type": "text", "text": t}


def _user(*parts: Obj) -> Obj:
    return {"role": "user", "content": list[JsonValue](parts)}


def _use(call_id: str) -> Obj:
    return {"type": "tool_use", "call_id": call_id, "name": "read_file", "input": {"path": "a"}}


def _result(call_id: str, t: str, late: bool = False) -> Obj:
    line: Obj = {"role": "tool", "call_id": call_id, "is_error": False, "content": [_text(t)]}
    return {**line, "late": True} if late else line


def _sent_use(call_id: str) -> Obj:
    return {"type": "tool_use", "id": call_id, "name": "read_file", "input": {"path": "a"}}


def _sent_result(call_id: str, t: str) -> Obj:
    return {"type": "tool_result", "tool_use_id": call_id, "is_error": False, "content": [_text(t)]}


def _document(citations: bool) -> Obj:
    block: Obj = {
        "type": "document",
        "source": {"type": "text", "media_type": "text/plain", "data": DOC.decode()},
        "title": "Colors",
    }
    return {**block, "citations": {"enabled": True}} if citations else block


# (name, system, tools, history lines, messages sent, citations setting)
type Base = tuple[str, str, list[JsonValue], list[JsonValue], list[JsonValue], bool]


def _bases() -> list[Base]:
    hi = _user(_text("hi"))
    sent_hi: Obj = {"role": "user", "content": [_text("hi")]}
    turn: list[JsonValue] = [
        hi,
        {"role": "assistant", "content": [_text("Hello!")]},
        _user(_text("Again?")),
    ]
    sent_turn: list[JsonValue] = [
        sent_hi,
        {"role": "assistant", "content": [_text("Hello!")]},
        {"role": "user", "content": [_text("Again?")]},
    ]
    uses: Obj = {"role": "assistant", "content": [_use("call_1"), _use("call_2")]}
    interleaved: list[JsonValue] = [
        _user(_text("Read a twice.")),
        uses,
        _result("call_1", "one"),
        _user(_text("Also check b.")),
        _result("call_2", "two"),
    ]
    sent_interleaved: list[JsonValue] = [
        {"role": "user", "content": [_text("Read a twice.")]},
        {"role": "assistant", "content": [_sent_use("call_1"), _sent_use("call_2")]},
        {
            "role": "user",
            "content": [
                _sent_result("call_1", "one"),
                _sent_result("call_2", "two"),
                _text("Also check b."),
            ],
        },
    ]
    late: list[JsonValue] = [
        hi,
        {"role": "assistant", "content": [_text("Waiting.")]},
        _result("call_0", "finished", late=True),
        _user(_text("Thanks.")),
    ]
    sent_late: list[JsonValue] = [
        sent_hi,
        {"role": "assistant", "content": [_text("Waiting.")]},
        {
            "role": "user",
            "content": [
                _text("[late tool result: call_id=call_0]"),
                _text("finished"),
                _text("Thanks."),
            ],
        },
    ]
    doc: Obj = {"type": "document_ref", "ref": DOC_REF, "title": "Colors"}
    asked = [_user(doc, _text("What color is the grass?"))]

    def sent_doc(citations: bool) -> list[JsonValue]:
        return [
            {"role": "user", "content": [_document(citations), _text("What color is the grass?")]}
        ]

    system = "Be brief."
    return [
        ("system-only", system, [], [hi], [sent_hi], False),
        ("tools-only", "", [TOOL], [hi], [sent_hi], False),
        ("system-and-tools", system, [TOOL], [hi], [sent_hi], False),
        ("two-turn-history", system, [], turn, sent_turn, False),
        ("interleaved-tool-results", system, [TOOL], interleaved, sent_interleaved, False),
        ("late-tool-result", system, [], late, sent_late, False),
        ("document", system, [], list[JsonValue](asked), sent_doc(False), False),
        ("document-citations", system, [], list[JsonValue](asked), sent_doc(True), True),
    ]


def _control(ttl: str) -> Obj:
    return {"type": "ephemeral"} if ttl == "5m" else {"type": "ephemeral", "ttl": "1h"}


def _cached(body: Obj, ttl: str | None) -> Obj:
    """The placement rule: top-level automatic caching for the history, and one explicit
    breakpoint at the end of line 0: on the system block, else on the last tool."""
    if ttl is None:
        return body
    cc = _control(ttl)
    out: Obj = {**body, "cache_control": cc}
    system = body.get("system")
    tools = body.get("tools")
    if isinstance(system, str):
        out["system"] = [{"type": "text", "text": system, "cache_control": cc}]
    elif isinstance(tools, list) and tools:
        last = tools[-1]
        if not isinstance(last, dict):
            raise AssertionError("a tool is an object")
        out["tools"] = [*tools[:-1], {**last, "cache_control": cc}]
    return out


def _case(base: Base, ttl: str | None) -> Obj:
    name, system, tools, history, messages, citations = base
    settings: Obj = {}
    if ttl is not None:
        settings["prompt_cache"] = ttl
    if citations:
        settings["citations"] = True
    head: Obj = {
        "adapter": {"name": "anthropic", "version": "1", "settings": settings},
        "model": MODEL,
        "params": PARAMS,
        "system": system,
        "tools": tools,
    }
    render = "".join(canonical(line).decode() + "\n" for line in (head, *history))
    body: Obj = {"model": MODEL["name"], **PARAMS, "messages": messages, "stream": True}
    if system:
        body["system"] = system
    if tools:
        body["tools"] = tools
    return {"name": f"{name}-{ttl or 'off'}", "render": render, "body": _cached(body, ttl)}


def _vector() -> str:
    doc: Obj = {
        "description": (
            "Anthropic Messages API request bodies (spec/schema/README.md, Cache controls): "
            "load `artifacts` (base64, named by sha256) into the model context, map `render` "
            "(Render v1 bytes) with the anthropic adapter, and compare the body to `body` as "
            "RFC 8785 canonical JSON."
        ),
        "artifacts": {sha(DOC): base64.b64encode(DOC).decode()},
        "cases": [_case(base, ttl) for base in _bases() for ttl in TTLS],
    }
    return dump(doc)


def write() -> None:
    VECTOR.write_text(_vector(), encoding="utf-8")


def check() -> list[str]:
    fresh = _vector()
    current = VECTOR.read_text(encoding="utf-8") if VECTOR.exists() else ""
    return [] if fresh == current else [f"{VECTOR.name}: differs; run gen_fixtures.py"]
