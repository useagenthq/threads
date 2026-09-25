"""Sandboxes: the adapter protocol, the fake provider, exec with output spilled at
the source, and provider resources tracked in the resource ledger."""

from threads.sandbox.exec import Command, run_exec
from threads.sandbox.fake import FakeSandbox, fake_sandbox
from threads.sandbox.protocol import (
    ExecOutput,
    ExecResult,
    LooksUpSandbox,
    LooksUpSnapshot,
    LookupSupport,
    Sandbox,
    SandboxError,
    SandboxId,
    SandboxInfo,
    SandboxSession,
    Trees,
)

__all__ = [
    "Command",
    "ExecOutput",
    "ExecResult",
    "FakeSandbox",
    "LooksUpSandbox",
    "LooksUpSnapshot",
    "LookupSupport",
    "Sandbox",
    "SandboxError",
    "SandboxId",
    "SandboxInfo",
    "SandboxSession",
    "Trees",
    "fake_sandbox",
    "run_exec",
]
