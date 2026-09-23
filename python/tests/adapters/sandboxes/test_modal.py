"""The Modal adapter over a fake Modal (modal_fake.py): the shared sandbox suite, the ledger's
crash and takeover rules, the fork conformance cases, and what only Modal needs."""

import asyncio

import pytest
from corpus import CASES, cases, load, obj
from fork_kit import assert_expected, run_case, script_of
from modal_fake import TOKEN_ID, TOKEN_SECRET, FakeModal, harness
from sandbox_backend import FakeBackend
from sandbox_contract import CHECKS, Check, run_check
from sandbox_kit import OPEN, KitContext
from sandbox_ledger_kit import LEDGER, Body, run_ledger

from threads.agents.config import ConfigError
from threads.modal import modal  # the re-export beside the `modal` SDK package
from threads.result import Err, Ok
from threads.sandbox.protocol import NO_ENV


@pytest.mark.parametrize("check", CHECKS, ids=lambda c: c.__name__)
def test_contract(check: Check) -> None:
    asyncio.run(run_check(check, harness, (TOKEN_ID, TOKEN_SECRET)))


@pytest.mark.parametrize("body", LEDGER, ids=lambda b: b.__name__)
def test_ledger(body: Body) -> None:
    asyncio.run(run_ledger(body, harness))


def _restores(name: str) -> bool:
    """Whether the case's fork reaches Sandbox.restore (it declares resources or a child)."""
    expected = load(CASES / name, "expected.json")
    return "resources" in expected or obj(expected["fork"])["child_created"] is True


@pytest.mark.parametrize("name", cases("fork"))
def test_fork_case(name: str) -> None:
    """Modal declares no snapshot capability, so a fork that reaches restore fails
    typed, snapshot_missing, before any provider call: no child, nothing created, every row
    released. A case that fails earlier holds exactly as the corpus says."""

    async def main() -> None:
        backend = FakeBackend.scripted(script_of(CASES / name))
        async with harness(backend, "fake") as sandbox:
            got = await run_case(CASES / name, sandbox, lambda: backend.creates)
        if not _restores(name):
            assert_expected(name, got)
            return
        assert got.error is not None
        assert (got.error.code, got.child_created, got.parent_unchanged) == (
            "snapshot_missing",
            False,
            True,
        )
        assert (got.creates, backend.requests) == (0, 0)
        assert all(state == "released" for _, state in got.rows)

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


def test_setup_refuses_missing_tokens_and_bad_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MODAL_TOKEN_ID", raising=False)
    monkeypatch.delenv("MODAL_TOKEN_SECRET", raising=False)
    with pytest.raises(ConfigError) as missing:
        modal(image_id="im-x")
    assert missing.value.code == "missing_secret"
    with pytest.raises(ConfigError) as bad:
        modal(image_id="im-x", token_id=TOKEN_ID, token_secret=TOKEN_SECRET, lifetime_ms=0)
    assert bad.value.code == "invalid_config"
    made = modal(image_id="im-x", token_id=TOKEN_ID, token_secret=TOKEN_SECRET, lifetime_ms=60_000)
    assert (made.lifetime_ms, made.info.capture_classes, made.info.termination) == (
        60_000,
        (),
        "unconfirmed",
    )
