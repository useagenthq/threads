# pyright: strict
"""The UI vectors (spec/schema/ui/README.md): `ui-thread-ids.json`, a chat key and principal to
the derived thread id, and `ui-inputs.json`, request bodies each route answers before any stream
starts, on a fresh host: accepted, or refused with its code. Computed here from the README."""

from __future__ import annotations

import hashlib
import re
from typing import TYPE_CHECKING

from .common import CASES
from .pieces import dump

if TYPE_CHECKING:
    from .jcs import JsonValue, Obj

IDS = CASES.parent / "vectors" / "ui-thread-ids.json"
INPUTS = CASES.parent / "vectors" / "ui-inputs.json"
KEY = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
ALICE: Obj = {"issuer": "api", "tenant": "acme", "subject": "alice"}
BOB: Obj = {"issuer": "api", "tenant": "acme", "subject": "bob"}
OTHER: Obj = {"issuer": "api", "tenant": "globex", "subject": "alice"}


def _part(s: str) -> str:
    return s.replace("%", "%25").replace("/", "%2F")


def _lp(s: str) -> bytes:
    b = s.encode()
    return len(b).to_bytes(4, "big") + b


def thread_id(principal: Obj, agent: str, key: str) -> str:
    pk = "/".join(_part(str(principal[k])) for k in ("issuer", "tenant", "subject"))
    digest = bytearray(
        hashlib.sha256(b"".join(map(_lp, ("threads-ui-v1", pk, agent, key)))).digest()[:16]
    )
    digest[6] = (digest[6] & 0x0F) | 0x80
    digest[8] = (digest[8] & 0x3F) | 0x80
    h = digest.hex()
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


def _ids() -> list[JsonValue]:
    rows: list[tuple[str, Obj, str, str]] = [
        ("a key", ALICE, "support", "chat-1"),
        ("the same key, another principal", BOB, "support", "chat-1"),
        ("the same key and subject, another tenant", OTHER, "support", "chat-1"),
        ("the same key, another agent", ALICE, "sales", "chat-1"),
        ("a lowercase UUID key", ALICE, "support", "0192a000-0000-7000-8000-000000000001"),
        ("a non-ASCII subject", {**ALICE, "subject": "zoë/ü%"}, "support", "chat-1"),
        ("a NUL inside a field", {**ALICE, "subject": "a\u0000b"}, "support", "c"),
        ("the split a NUL would collide with", {**ALICE, "subject": "a"}, "support", "b\u0000c"),
        ("a slash in the issuer", {**ALICE, "issuer": "x/y"}, "support", "k"),
        ("an escaped slash as text", {**ALICE, "issuer": "x%2Fy"}, "support", "k"),
        ("a 128-character key", ALICE, "support", "k" * 128),
        ("a 129-character key", ALICE, "support", "k" * 129),
        ("a key with a space", ALICE, "support", "chat 1"),
        ("an empty key", ALICE, "support", ""),
        ("a key with a colon", ALICE, "support", "chat:1"),
    ]
    out: list[JsonValue] = []
    for name, principal, agent, key in rows:
        entry: Obj = {"name": name, "principal": principal, "agent": agent, "key": key}
        if KEY.match(key):
            entry["thread_id"] = thread_id(principal, agent, key)
        else:
            entry["error"] = "invalid_request"
        out.append(entry)
    return out


def _user(mid: str, *parts: Obj) -> Obj:
    listed: list[JsonValue] = [*parts]
    return {"id": mid, "role": "user", "parts": listed, "metadata": {"x": 1}}


def _chat(**more: JsonValue) -> Obj:
    body: Obj = {
        "id": "chat-1",
        "messages": [_user("m-1", {"type": "text", "text": "Hi", "state": "done"})],
        "trigger": "submit-message",
        "extra": True,
    }
    return {**body, **more}


def _run(**more: JsonValue) -> Obj:
    body: Obj = {
        "threadId": "chat-1",
        "runId": "run-1",
        "messages": [{"id": "m-1", "role": "user", "content": "Hi"}],
        "tools": [],
        "context": [],
        "state": {"any": "thing"},
        "forwardedProps": {"x": 1},
        "protocolVersion": "1.0",
    }
    return {**body, **more}


def _inputs() -> list[JsonValue]:
    bad = "invalid_request"
    rows: list[tuple[str, str, Obj, str | None]] = [
        ("the accepted AI SDK subset, other fields ignored", "ai-sdk", _chat(), None),
        ("regenerate", "ai-sdk", _chat(trigger="regenerate-message"), bad),
        (
            "a file part",
            "ai-sdk",
            _chat(messages=[_user("m-1", {"type": "file", "url": "x", "mediaType": "image/png"})]),
            bad,
        ),
        ("a chat key with a space", "ai-sdk", _chat(id="chat 1"), bad),
        (
            "a message id with a space",
            "ai-sdk",
            _chat(messages=[_user("m 1", {"type": "text", "text": "Hi"})]),
            bad,
        ),
        ("no messages", "ai-sdk", _chat(messages=[]), bad),
        (
            "an empty text",
            "ai-sdk",
            _chat(messages=[_user("m-1", {"type": "text", "text": ""})]),
            bad,
        ),
        (
            "a system message last",
            "ai-sdk",
            _chat(
                messages=[{"id": "s", "role": "system", "parts": [{"type": "text", "text": "x"}]}]
            ),
            bad,
        ),
        ("an unknown trigger", "ai-sdk", _chat(trigger="resume-stream"), bad),
        ("the accepted AG-UI subset, state and context ignored", "ag-ui", _run(), None),
        (
            "a text-parts user message",
            "ag-ui",
            _run(
                messages=[
                    {"id": "m-1", "role": "user", "content": [{"type": "text", "text": "Hi"}]}
                ]
            ),
            None,
        ),
        (
            "frontend tools",
            "ag-ui",
            _run(tools=[{"name": "t", "description": "d", "parameters": {}}]),
            bad,
        ),
        (
            "a binary content part",
            "ag-ui",
            _run(
                messages=[
                    {
                        "id": "m-1",
                        "role": "user",
                        "content": [{"type": "binary", "mimeType": "image/png"}],
                    }
                ]
            ),
            bad,
        ),
        ("no user message and no resume", "ag-ui", _run(messages=[]), bad),
        ("no runId", "ag-ui", {k: v for k, v in _run().items() if k != "runId"}, bad),
        (
            "a resume status that is not resolved or cancelled",
            "ag-ui",
            _run(resume=[{"interruptId": "x", "status": "approved"}]),
            bad,
        ),
        (
            "a resume entry naming no interrupt of the thread",
            "ag-ui",
            _run(resume=[{"interruptId": "x", "status": "cancelled"}]),
            "not_found",
        ),
    ]
    return [{"name": n, "protocol": p, "body": b, "code": c} for n, p, b, c in rows]


def write() -> None:
    IDS.write_text(dump(_ids()))
    INPUTS.write_text(dump(_inputs()))


def check() -> list[str]:
    return [
        f"{path.name} is stale; run gen_fixtures.py"
        for path, made in ((IDS, _ids()), (INPUTS, _inputs()))
        if not path.is_file() or path.read_text() != dump(made)
    ]
