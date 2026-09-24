"""The remote kit over an in-memory driver (remote_driver.py): the shared sandbox contract, the
fork cases and the ledger suite, before any provider's wire format is involved; and the rule
that a driver declares a final lookup or confirmed termination only with its proof."""

import asyncio

import pytest
from corpus import CASES, cases
from fork_kit import assert_expected, run_case, script_of
from remote_driver import MemoryDriver, serve
from sandbox_backend import FakeBackend
from sandbox_contract import CHECKS, Check, run_check
from sandbox_deadline_kit import DEADLINE
from sandbox_kit import OPEN, KitContext
from sandbox_ledger_kit import LEDGER, Body, run_ledger

from threads.adapters.sandboxes import fence
from threads.loop.model import Found, LookupResult, NotFound
from threads.loop.tools import Termination
from threads.result import Err, Ok
from threads.sandbox.remote.driver import Confirmed, FinalLookup, Unconfirmed
from threads.sandbox.remote.sandbox import RemoteInfo, RemoteSandbox

INFO = RemoteInfo("memory", "enforced")


@pytest.mark.parametrize("check", [*CHECKS, *DEADLINE], ids=lambda c: c.__name__)
def test_contract(check: Check) -> None:
    asyncio.run(run_check(check, serve, ()))


@pytest.mark.parametrize("body", LEDGER, ids=lambda b: b.__name__)
def test_ledger(body: Body) -> None:
    asyncio.run(run_ledger(body, serve))


@pytest.mark.parametrize("name", cases("fork"))
def test_fork_case(name: str) -> None:
    async def main() -> None:
        backend = FakeBackend.scripted(script_of(CASES / name))
        async with serve(backend, "fake") as sandbox:
            assert_expected(name, await run_case(CASES / name, sandbox, lambda: backend.creates))

    asyncio.run(main())


def test_undeclared_a_driver_is_nonfinal_and_unconfirmed() -> None:
    info = RemoteSandbox(MemoryDriver(FakeBackend()), INFO).info
    assert (info.lookup.create, info.lookup.snapshot, info.termination) == (
        "nonfinal",
        "none",
        "unconfirmed",
    )


def test_a_final_lookup_is_declared_and_its_absence_is_final() -> None:
    driver = MemoryDriver(FakeBackend())

    async def find(key: str) -> LookupResult[str]:
        found = await driver.find(key)
        return NotFound() if found.status == "not_found_nonfinal" else found

    driver.lookup = FinalLookup(find)
    sandbox = RemoteSandbox(driver, INFO)
    assert sandbox.info.lookup.create == "final"

    async def main() -> None:
        assert await sandbox.lookup("never", OPEN) == Ok(NotFound())
        made = await sandbox.create("k", OPEN)
        assert isinstance(made, Ok)
        found = await sandbox.lookup("k", OPEN)
        assert isinstance(found, Ok)
        assert isinstance(found.value, Found)
        assert found.value.value.id == made.value.id

    asyncio.run(main())


def test_confirmed_termination_answers_what_the_driver_proves() -> None:
    """The kit uses the driver's terminate, not the best-effort stop, and returns its answer."""
    driver = MemoryDriver(FakeBackend())
    asked: list[tuple[str, str]] = []

    async def terminate(sandbox_id: str, process_key: str) -> Termination:
        await fence.check()  # the provider's control plane: fenced like any send
        asked.append((sandbox_id, process_key))
        return "already_exited" if process_key == "done" else "terminated"

    async def never(_sandbox_id: str, _process_key: str) -> None:
        raise AssertionError("a confirmed driver's best-effort stop is never used")

    driver.termination = Confirmed(terminate)
    sandbox = RemoteSandbox(driver, INFO)
    assert sandbox.info.termination == "confirmed"

    async def main() -> None:
        made = await sandbox.create("k", OPEN)
        assert isinstance(made, Ok)
        box = made.value
        assert await box.terminate("run", OPEN) == Ok("terminated")
        assert await box.terminate("done", OPEN) == Ok("already_exited")
        assert asked == [(box.id, "run"), (box.id, "done")]
        refused = await box.terminate("run", KitContext(live=False))
        assert isinstance(refused, Err)
        assert refused.error.code == "stale_epoch"

    asyncio.run(main())
    # The proof is the declaration: neither is constructible without its callable, and a
    # confirmed declaration can't carry the best-effort stop.
    Confirmed(never)  # pyright: ignore[reportArgumentType] - a stop proves no termination
    Unconfirmed(never)
