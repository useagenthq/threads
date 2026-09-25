"""Lane 16 A1: a sandbox_local built-in is sandbox_local only under deny-all egress. Under open
egress bash, write, edit and notebook_edit pin unguarded, so a call whose outcome is in doubt
parks rather than being settled by the sandbox."""

import asyncio
import json
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest
from local_sandbox import LocalSandbox, LocalSession
from pydantic import JsonValue

from threads import Parked, agent, scripted_model, sqlite
from threads.log import Permissions, ThreadStartedEvent
from threads.result import Err, Ok
from threads.sandbox.protocol import NO_ENV, ExecOutput, SandboxContext, SandboxError

USAGE: JsonValue = {"input_tokens": 1, "output_tokens": 1}
NOTEBOOK: JsonValue = {
    "cells": [{"id": "a", "cell_type": "markdown", "source": "# T", "metadata": {}}],
    "metadata": {},
    "nbformat": 4,
    "nbformat_minor": 5,
}
CALLS: dict[str, JsonValue] = {
    "bash": {"command": "echo hi"},
    "edit": {"path": "a.txt", "old_string": "old", "new_string": "new"},
    "notebook_edit": {"path": "n.ipynb", "cell_id": "a", "new_source": "# U"},
    "write": {"path": "a.txt", "content": "x"},
}


class InDoubt(LocalSession):
    """Every upload's answer is lost and every exec times out."""

    async def exec(  # noqa: PLR0913 - the protocol's options
        self,
        command: Sequence[str],
        context: SandboxContext,
        *,
        process_key: str,
        cwd: str = "/workspace",
        env: Mapping[str, str] = NO_ENV,
        timeout_ms: int | None = None,
        stdin: bytes | None = None,
    ) -> Ok[ExecOutput] | Err[SandboxError]:
        return Err(SandboxError("timeout", "deadline"))

    async def upload(
        self, path: str, data: bytes, context: SandboxContext
    ) -> Ok[None] | Err[SandboxError]:
        return Err(SandboxError("unavailable", "lost"))


def _allow(name: str) -> Permissions:
    return Permissions.model_validate(
        {
            "mode": "default",
            "allow": [name],
            "ask": [],
            "deny": [],
            "protected_paths": [".git/**"],
            "allow_bypass": False,
            "plan_exit_mode": "default",
        }
    )


@pytest.mark.parametrize("name", sorted(CALLS))
def test_a_sandbox_mutator_pins_unguarded_and_an_in_doubt_call_parks(
    tmp_path: Path, name: str
) -> None:
    (tmp_path / "a.txt").write_text("old")
    (tmp_path / "n.ipynb").write_text(json.dumps(NOTEBOOK))
    box = LocalSandbox(tmp_path)
    box.session = InDoubt(tmp_path)
    part: JsonValue = {"type": "tool_use", "call_id": "c1", "name": name, "input": CALLS[name]}
    call: JsonValue = {"content": [part], "stop_reason": "tool_use", "usage": USAGE}
    bot = agent(
        model=scripted_model({"responses": [call]}),
        sandbox=box,
        egress="unenforced",
        permissions=_allow(name),
    )

    async def main() -> None:
        result = await bot.run("go", store=sqlite(":memory:"))
        assert isinstance(result, Parked)
        timeline = await result.thread.timeline()
        assert isinstance(timeline, Ok)
        events = [e.event for e in timeline.value.entries]
        started = next(e for e in events if isinstance(e, ThreadStartedEvent)).data
        classes = {t.name: t.effect_class for t in started.tools if t.name in CALLS}
        assert classes == dict.fromkeys(CALLS, "unguarded")
        kinds = [e.type for e in events]
        assert "effect_unknown" in kinds
        assert "effect_resolved" not in kinds
        assert "tool_result" not in kinds

    asyncio.run(main())
