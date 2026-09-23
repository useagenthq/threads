"""The Modal adapter over a fake Modal (modal_fake.py): the shared sandbox suite, the ledger's
crash and takeover rules, the fork conformance cases, and what only Modal needs."""

import asyncio

import pytest
from corpus import CASES, cases
from fork_kit import assert_expected, assert_restore_refused, reaches_restore, run_case, script_of
from modal_fake import TOKEN_ID, TOKEN_SECRET, FakeModal, harness
from sandbox_backend import FakeBackend
from sandbox_contract import CHECKS, Check, run_check
from sandbox_deadline_kit import DEADLINE
from sandbox_kit import OPEN, KitContext
from sandbox_ledger_kit import LEDGER, Body, run_ledger

from threads.agents.config import ConfigError
from threads.modal import modal  # the re-export beside the `modal` SDK package
from threads.result import Err, Ok
from threads.sandbox.protocol import NO_ENV


@pytest.mark.parametrize("check", [*CHECKS, *DEADLINE], ids=lambda c: c.__name__)
def test_contract(check: Check) -> None:
    asyncio.run(run_check(check, harness, (TOKEN_ID, TOKEN_SECRET)))


@pytest.mark.parametrize("body", LEDGER, ids=lambda b: b.__name__)
def test_ledger(body: Body) -> None:
    asyncio.run(run_ledger(body, harness))


@pytest.mark.parametrize("name", cases("fork"))
def test_fork_case(name: str) -> None:
    """Modal declares no snapshot capability, so a fork that reaches restore fails
    typed, snapshot_missing, before any provider call: no child, nothing created, every row
    released. A case that fails earlier holds exactly as the corpus says."""

    async def main() -> None:
        backend = FakeBackend.scripted(script_of(CASES / name))
        async with harness(backend, "fake") as sandbox:
            got = await run_case(CASES / name, sandbox, lambda: backend.creates)
        if not reaches_restore(name):
            assert_expected(name, got)
            return
        assert_restore_refused(got)
        assert backend.requests == 0

    asyncio.run(main())


def test_a_refused_fence_sends_no_request() -> None:
    async def main() -> None:
        backend = FakeBackend.scripted()
        async with harness(backend, "modal") as sandbox:
            stale = KitContext(live=False)
            refused = await sandbox.create("k", stale)
            assert isinstance(refused, Err)
            assert refused.error.code == "stale_epoch"
            assert (backend.requests, backend.creates, stale.fences) == (0, 0, 1)

    asyncio.run(main())


def test_invalid_responses_are_typed_never_a_crash() -> None:
    async def main() -> None:
        backend = FakeBackend.scripted()
        fake = FakeModal(backend)
        fake.sandbox_ids["threads-k-empty"] = ""
        async with harness(backend, "modal", fake) as sandbox:
            empty = await sandbox.create("k-empty", OPEN)
            assert isinstance(empty, Err)
            assert empty.error.code == "unavailable"
            made = await sandbox.create("k", OPEN)
            assert isinstance(made, Ok)
            fake.router_url = "http://plain.modal.test"  # a router that isn't https
            ran = await made.value.exec(["echo"], OPEN, process_key="p", env=NO_ENV)
            assert isinstance(ran, Err)
            assert ran.error.code == "unavailable"

    asyncio.run(main())


def test_the_factory_refuses_bad_config() -> None:
    """Missing tokens are a setup error at check() or the first run (test_credentials.py)."""
    with pytest.raises(ConfigError) as bad:
        modal(image_id="im-x", token_id=TOKEN_ID, token_secret=TOKEN_SECRET, lifetime_ms=0)
    assert bad.value.code == "invalid_config"
    made = modal(image_id="im-x", token_id=TOKEN_ID, token_secret=TOKEN_SECRET, lifetime_ms=60_000)
    assert (made.lifetime_ms, made.info.capture_classes, made.info.termination) == (
        60_000,
        (),
        "unconfirmed",
    )


def test_egress_is_denied_unless_the_internet_is_allowed() -> None:
    async def main() -> None:
        backend = FakeBackend.scripted()
        fake = FakeModal(backend)
        async with harness(backend, "modal", fake) as sandbox:
            assert sandbox.info.egress == "enforced"
            assert isinstance(await sandbox.create("k", OPEN), Ok)
        assert fake.blocked == [True]
        opened = modal(
            image_id="im-x", token_id=TOKEN_ID, token_secret=TOKEN_SECRET, allow_internet=True
        )
        assert opened.info.egress == "unenforced"

    asyncio.run(main())
