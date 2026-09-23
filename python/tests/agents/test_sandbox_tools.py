"""agent(sandbox=...) end to end: built-ins pinned in line 0, the permission fold decides before
any dispatch, a deadline is effect_unknown then interrupted, and a host secret never reaches
the log, a request or the sandbox (invariant 4)."""

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest
from local_sandbox import LOCAL_INFO, LocalSandbox
from pydantic import BaseModel, JsonValue
from pydantic.experimental.missing_sentinel import MISSING

from threads import Completed, ConfigError, Parked, RunContext, agent, scripted_model, sqlite, tool
from threads.agents.definition import Definition
from threads.log import (
    EffectUnknownEvent,
    Event,
    Permissions,
    ThreadStartedEvent,
    ToolResultEvent,
)
from threads.result import Ok
from threads.secrets import resolve, secret

USAGE: JsonValue = {"input_tokens": 10, "output_tokens": 2}
CANARY = "canary-3f9c1e7b"


def text(reply: str) -> JsonValue:
    return {"content": [{"type": "text", "text": reply}], "stop_reason": "end_turn", "usage": USAGE}


def use(name: str, args: JsonValue, call_id: str = "call_1") -> JsonValue:
    part: JsonValue = {"type": "tool_use", "call_id": call_id, "name": name, "input": args}
    return {"content": [part], "stop_reason": "tool_use", "usage": USAGE}


def perms(mode: str) -> Permissions:
    return Permissions.model_validate(
        {
            "mode": mode,
            "allow": [],
            "ask": [],
            "deny": [],
            "protected_paths": [".git/**"],
            "allow_bypass": False,
            "plan_exit_mode": "default",
        }
    )


async def events(result: Completed[str]) -> list[Event]:
    timeline = await result.thread.timeline()
    assert isinstance(timeline, Ok)
    return [e.event for e in timeline.value.entries]


def test_builtins_are_pinned_first_and_bash_runs_in_the_sandbox(tmp_path: Path) -> None:
    box = LocalSandbox(tmp_path / "ws")
    (tmp_path / "ws").mkdir()
    model = scripted_model({"responses": [use("bash", {"command": "echo hi"}), text("Done.")]})
    bot = agent(model=model, sandbox=box, permissions=perms("bypass"))
    store = str(tmp_path / "db" / "threads.db")

    async def main() -> None:
        result = await bot.run("go", store=sqlite(store))
        assert isinstance(result, Completed)
        logged = await events(result)
        started = next(e for e in logged if isinstance(e, ThreadStartedEvent)).data
        names = [t.name for t in started.tools]
        assert names == ["bash", "edit", "glob", "grep", "ls", "read", "read_tool_result", "write"]
        assert started.sandbox_provider == "local"
        done = next(e for e in logged if isinstance(e, ToolResultEvent))
        assert json.loads(done.data.preview)["stdout"] == "hi\n"
        assert box.created == 1

    asyncio.run(main())


@pytest.mark.parametrize(
    ("mode", "path", "written"),
    [
        ("default", "a.txt", False),
        ("accept_edits", "a.txt", True),
        ("accept_edits", ".git/config", False),
    ],
)
def test_permission_modes_decide_before_dispatch(
    tmp_path: Path, mode: str, path: str, written: bool
) -> None:
    box = LocalSandbox(tmp_path)
    call = use("write", {"path": path, "content": "x"})
    bot = agent(
        model=scripted_model({"responses": [call, text("Done.")]}),
        sandbox=box,
        permissions=perms(mode),
    )

    async def main() -> None:
        result = await bot.run("go", store=sqlite(":memory:"))
        assert isinstance(result, Completed if written else Parked)
        assert (tmp_path / path).exists() is written

    asyncio.run(main())


def test_a_bash_deadline_is_effect_unknown_then_interrupted(tmp_path: Path) -> None:
    box = LocalSandbox(tmp_path)
    call = use("bash", {"command": "sleep 30", "timeout_ms": 50})
    bot = agent(
        model=scripted_model({"responses": [call, text("Done.")]}),
        sandbox=box,
        permissions=perms("bypass"),
    )
    store = str(tmp_path / "db" / "threads.db")

    async def main() -> None:
        result = await bot.run("go", store=sqlite(store))
        assert isinstance(result, Completed)
        logged = await events(result)
        kinds = [e.type for e in logged]
        at = kinds.index("effect_begin")
        assert kinds[at : at + 4] == [
            "effect_begin",
            "effect_unknown",
            "effect_resolved",
            "tool_result",
        ]
        unknown, result_event = logged[at + 1], logged[at + 3]
        assert isinstance(unknown, EffectUnknownEvent)
        assert unknown.data.reason == "timeout"
        assert isinstance(result_event, ToolResultEvent)
        assert result_event.data.origin == "interrupted"

    asyncio.run(main())


def test_a_provider_without_egress_enforcement_needs_an_opt_in(tmp_path: Path) -> None:
    loose = LocalSandbox(tmp_path, replace(LOCAL_INFO, egress="unenforced"))
    model = scripted_model({"responses": []})
    with pytest.raises(ConfigError) as raised:
        agent(model=model, sandbox=loose)
    assert raised.value.code == "egress_policy_unsupported"
    agent(model=model, sandbox=loose, egress="unenforced")
    specs = Definition("a", model, "", (), sandbox=loose, egress="unenforced").specs()
    assert next(s for s in specs if s.name == "bash").effect_class == "unguarded"


def test_read_tool_result_reads_spilled_output_back(tmp_path: Path) -> None:
    box = LocalSandbox(tmp_path)
    read_back: JsonValue = {"call_id": "call_1", "offset": 0, "length": 6}
    script: JsonValue = {
        "responses": [
            use("bash", {"command": "seq 1 20000"}),
            use("read_tool_result", read_back, "call_2"),
            text("Done."),
        ]
    }
    bot = agent(model=scripted_model(script), sandbox=box, permissions=perms("bypass"))

    async def main() -> None:
        result = await bot.run("go", store=sqlite(str(tmp_path / "db" / "threads.db")))
        assert isinstance(result, Completed)
        results = [e for e in await events(result) if isinstance(e, ToolResultEvent)]
        spilled, read = results
        assert spilled.data.ref is not MISSING
        size = spilled.data.ref.bytes
        assert read.data.preview == f"[bytes 0-6 of {size}]\n1\n2\n3\n"

    asyncio.run(main())


class Probe(BaseModel):
    url: str


def test_a_host_secret_never_reaches_the_log_a_request_or_the_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("THREADS_TEST_TOKEN", CANARY)
    token = secret("THREADS_TEST_TOKEN")
    seen: list[str] = []

    async def call_service(args: Probe, _ctx: RunContext[None]) -> str:
        # Host code resolves the secret at use; only the service's answer is recorded.
        seen.append(f"Bearer {resolve(token)}")
        return f"200 OK from {args.url}"

    service = tool(
        name="service",
        description="Call the service.",
        input=Probe,
        runs="host",
        execute=call_service,
        effect="read_only",
    )
    box = LocalSandbox(tmp_path / "ws")
    (tmp_path / "ws").mkdir()
    script: JsonValue = {
        "responses": [
            use("service", {"url": "https://api.example"}),
            use("bash", {"command": "env; cat /proc/self/environ 2>/dev/null"}, "call_2"),
            text("Done."),
        ]
    }
    bot = agent(
        model=scripted_model(script),
        tools=[service],
        sandbox=box,
        permissions=perms("bypass"),
        instructions=f"Use {token!r}.",
    )
    store = tmp_path / "db" / "threads.db"

    async def main() -> None:
        result = await bot.run("go", store=sqlite(str(store)), deps=None)
        assert isinstance(result, Completed)

    asyncio.run(main())
    assert seen == [f"Bearer {CANARY}"]
    stored = [p for p in (tmp_path / "db").rglob("*") if p.is_file()]
    assert stored
    for path in stored:
        assert CANARY.encode() not in path.read_bytes(), path
    assert box.session.envs
    assert all(env == {} for env in box.session.envs)
