"""What tools built on the sandbox session need from the runner (`SandboxTools` is one): the
session, its fenced context, and framework control commands whose output the host reads."""

from collections.abc import Sequence
from typing import Protocol

from threads.log import ArtifactRef
from threads.loop.tools import Dispatched
from threads.result import Err, Ok
from threads.sandbox.protocol import ExecResult, SandboxContext, SandboxSession


class Control(Protocol):
    @property
    def context(self) -> SandboxContext: ...

    async def session(self) -> SandboxSession | None: ...

    async def put(self, data: bytes, media_type: str) -> ArtifactRef:
        """Stores bytes as a durable artifact."""
        ...

    async def command(
        self, argv: Sequence[str], key: str, timeout_ms: int = ...
    ) -> Ok[ExecResult] | Err[Dispatched]: ...
