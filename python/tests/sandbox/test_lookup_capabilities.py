"""A sandbox's two lookups are optional capabilities (`LooksUpSandbox`, `LooksUpSnapshot`), each
declared by its own `info.lookup` operation: a sandbox with neither still runs, captures and
forks; one that declares a lookup without its method is refused by check()."""

import asyncio

from partial_sandbox import CreateLookupOnly, NoLookups, SnapshotLookupOnly
from pydantic import JsonValue

from threads import (
    Completed,
    LooksUpSandbox,
    LooksUpSnapshot,
    Sandbox,
    agent,
    fake_sandbox,
    scripted_model,
    sqlite,
)
from threads.adapters.channels.whatsapp import WhatsAppChannel
from threads.adapters.sandboxes.daytona.sandbox import DaytonaSandbox
from threads.adapters.sandboxes.e2b.sandbox import E2BSandbox
from threads.adapters.sandboxes.modal.sandbox import ModalSandbox
from threads.host import Challenged
from threads.log import Permissions
from threads.result import Err, Ok
from threads.sandbox import LookupSupport

USAGE: JsonValue = {"input_tokens": 10, "output_tokens": 2}
BYPASS = Permissions(
    mode="bypass",
    allow=[],
    ask=[],
    deny=[],
    protected_paths=[],
    allow_bypass=True,
    plan_exit_mode="default",
)
WRITE: JsonValue = {
    "content": [
        {
            "type": "tool_use",
            "call_id": "c1",
            "name": "write",
            "input": {"path": "a.txt", "content": "A"},
        }
    ],
    "stop_reason": "tool_use",
    "usage": USAGE,
}
DONE: JsonValue = {
    "content": [{"type": "text", "text": "Done."}],
    "stop_reason": "end_turn",
    "usage": USAGE,
}


def test_a_sandbox_without_lookups_runs_captures_and_forks() -> None:
    async def main() -> None:
        sandbox: Sandbox = NoLookups(fake_sandbox())
        assert not isinstance(sandbox, LooksUpSandbox | LooksUpSnapshot)
        bot = agent(
            model=scripted_model({"responses": [WRITE, DONE]}), sandbox=sandbox, permissions=BYPASS
        )
        result = await bot.run("go", store=sqlite(":memory:"))
        assert isinstance(result, Completed)
        points = await result.thread.fork_points()
        assert isinstance(points, Ok)
        assert len(points.value) == 1
        child = await result.thread.fork(points.value[0])
        assert isinstance(child, Ok), child

    asyncio.run(main())


def check(sandbox: Sandbox) -> Ok[None] | Err[str]:
    checked = asyncio.run(agent(model=scripted_model({"responses": []}), sandbox=sandbox).check())
    if isinstance(checked, Ok):
        return Ok(None)
    assert checked.error.code == "capability_missing"
    return Err(checked.error.message)


def test_each_declared_lookup_needs_its_own_method() -> None:
    create = LookupSupport(create="final", snapshot="none")
    snapshot = LookupSupport(create="none", snapshot="final")
    assert check(CreateLookupOnly(fake_sandbox(), create)) == Ok(None)
    assert check(SnapshotLookupOnly(fake_sandbox(), snapshot)) == Ok(None)
    assert check(NoLookups(fake_sandbox(), create)) == Err(
        "sandbox fake declares lookup.create 'final' but has no lookup method: "
        "implement lookup, or declare lookup.create='none'"
    )
    assert check(
        CreateLookupOnly(fake_sandbox(), LookupSupport(create="final", snapshot="nonfinal"))
    ) == Err(
        "sandbox fake declares lookup.snapshot 'nonfinal' but has no lookup_snapshot method: "
        "implement lookup_snapshot, or declare lookup.snapshot='none'"
    )


def test_the_fake_and_bundled_sandboxes_implement_both_lookups() -> None:
    assert isinstance(fake_sandbox(), LooksUpSandbox)
    assert isinstance(fake_sandbox(), LooksUpSnapshot)
    for bundled in (DaytonaSandbox, E2BSandbox, ModalSandbox):
        assert issubclass(bundled, LooksUpSandbox)
        assert issubclass(bundled, LooksUpSnapshot)


def test_challenged_is_exported() -> None:
    assert issubclass(WhatsAppChannel, Challenged)
