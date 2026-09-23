"""Offline kit for model adapter tests: a ModelContext over in-memory artifacts, conformance
render cases re-pinned to an adapter, and an HTTP transport that answers from a script."""

import json
import os
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import httpx2
from corpus import CASES
from pydantic import JsonValue

from threads.log import ArtifactRef, BranchId, ParseError
from threads.log.digest import sha256_hex
from threads.loop.model import ModelChunk, ModelContext, ModelRequest
from threads.result import Err, Ok

GOLDEN = Path(__file__).parent / "golden"
BRANCH = BranchId("0192b000-0000-7000-8000-000000000001")


@dataclass
class FakeContext:
    """spec/api.json `ModelContext` over a dict; `owner` False makes every fence fail."""

    artifacts: dict[str, bytes] = field(default_factory=dict[str, bytes])
    owner: bool = True
    fences: int = 0
    branch_id: BranchId = field(default_factory=lambda: BRANCH)
    epoch: int = 1

    async def fence(self) -> Ok[None] | Err[ParseError]:
        self.fences += 1
        return Ok(None) if self.owner else Err(ParseError("stale_epoch", "the lease moved"))

    async def read(self, ref: ArtifactRef) -> Ok[bytes] | Err[ParseError]:
        data = self.artifacts.get(ref.sha256)
        if data is None:
            return Err(ParseError("artifact_missing", ref.sha256))
        if len(data) != ref.bytes or sha256_hex(data) != ref.sha256:
            return Err(ParseError("artifact_corrupt", ref.sha256))
        return Ok(data)

    async def put(self, data: bytes, media_type: str) -> ArtifactRef:
        sha = sha256_hex(data)
        self.artifacts[sha] = data
        return ArtifactRef(sha256=sha, bytes=len(data), media_type=media_type)


def render_case(name: str, adapter: str, provider: str) -> tuple[bytes, FakeContext]:
    """A conformance case's next request, re-pinned from the scripted adapter to `adapter`."""
    case = CASES / name
    body = (case / "request.bytes").read_bytes()
    body = body.replace(b'"name":"scripted"', f'"name":"{adapter}"'.encode())
    body = body.replace(b'"provider":"scripted"', f'"provider":"{provider}"'.encode())
    blobs = {p.name: p.read_bytes() for p in (case / "artifacts").iterdir()}
    return body, FakeContext(blobs)


def line(value: JsonValue) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode() + b"\n"


@dataclass
class Script:
    """Answers each HTTP request with the next scripted response and keeps what was sent."""

    responses: list[httpx2.Response]
    sent: list[httpx2.Request] = field(default_factory=list[httpx2.Request])

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.sent.append(request)
        return self.responses.pop(0)

    def bodies(self) -> list[JsonValue]:
        return [json.loads(r.content) for r in self.sent]


def sse(events: Sequence[tuple[str | None, JsonValue]]) -> httpx2.Response:
    raw = b""
    for name, data in events:
        if name is not None:
            raw += f"event: {name}\n".encode()
        raw += b"data: " + json.dumps(data).encode() + b"\n\n"
    return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=raw)


async def collect(
    send: Callable[[ModelRequest, ModelContext], AsyncIterator[ModelChunk]],
    body: bytes,
    context: ModelContext,
) -> list[ModelChunk]:
    return [c async for c in send(ModelRequest("b:e", body), context)]


def golden(path: str, value: JsonValue) -> None:
    """Compares against a checked-in snapshot. THREADS_WRITE_GOLDEN=1 writes it, to review."""
    file = GOLDEN / path
    text = json.dumps(value, indent=2, sort_keys=True) + "\n"
    if os.environ.get("THREADS_WRITE_GOLDEN") == "1":
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(text)
    assert file.read_text() == text
