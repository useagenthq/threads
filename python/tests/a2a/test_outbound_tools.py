"""What the two tools declare, and the refusals that happen before any byte leaves.

The declared contract is asserted here rather than only through behaviour: a send whose finality
flipped to "final" would re-dispatch under the same key, which is the one thing invariant 3 forbids.

This mirrors typescript/packages/a2a/test/outbound/tools.test.ts."""

import asyncio

from outbound_kit import CARD_URL, TOOL, Answer, Drill, Partner, drill, task, text, use
from pydantic.experimental.missing_sentinel import MISSING

from threads.a2a.outbound.tools import FINALITY
from threads.a2a.protocol import PROVENANCE
from threads.a2a.remote import bearer, remote
from threads.log import RemoteCallEvent, ToolResultEvent
from threads.loop.drive import drive


def test_the_send_is_reconcilable_and_nonfinal_and_the_status_read_is_read_only() -> None:
    r = remote("refunds", CARD_URL, transport=Partner(), resolve=Partner().resolve)
    send, status = r.tools(name=TOOL, description="Ask the desk.")
    assert (send.name, status.name) == (TOOL, f"{TOOL}_status")
    assert send.spec().effect_class == "reconcilable"
    # Not idempotent, and no window: the window comes from a partner's card, unknown here.
    assert send.spec().dedup_window_ms is MISSING
    assert status.spec().effect_class == "read_only"
    # A lookup that finds nothing never proves absence, so the declared finality is nonfinal.
    assert FINALITY == "nonfinal"


def test_a_task_this_conversation_did_not_create_is_refused_before_anything_is_sent() -> None:
    async def main() -> Drill:
        p = Partner()
        # The model names a task id of its own invention.
        d = await drill(
            p, [use(message="what about this one?", task_id="someone-elses"), text("ok")]
        )
        await drive(d.rt)
        return d

    d = asyncio.run(main())
    # Nothing began, nothing was stored, and nothing was sent.
    assert "remote_call" not in d.kinds()
    assert "effect_begin" not in d.kinds()
    result = d.last("tool_result")
    assert isinstance(result, ToolResultEvent)
    assert result.data.is_error is True
    assert result.data.origin == "not_executed"
    assert "unknown_task" in result.data.preview
    assert d.partner.sends() == []


def test_the_credential_rides_in_the_header_and_reaches_no_stored_byte() -> None:
    # The canary a stored byte must not hold.
    token = "s3cret-partner-token"  # noqa: S105

    async def main() -> tuple[Drill, bytes]:
        p = Partner()
        p.answer = Answer("task", task("task-1", "TASK_STATE_COMPLETED", "paid"))
        d = await drill(p, [use(message="ask"), text("ok")], auth=bearer(token))
        await drive(d.rt)
        call = d.last("remote_call")
        assert isinstance(call, RemoteCallEvent)
        got = await d.store.get_artifact(call.data.request_ref.sha256)
        return d, getattr(got, "value", b"")

    d, stored = asyncio.run(main())
    sent = d.partner.sends()[0]
    assert sent.headers["Authorization"] == f"Bearer {token}"
    # The stored request bytes are what a re-dispatch replays, so the token must not be in them.
    assert token not in (sent.body or "")
    assert token not in stored.decode()


def test_the_opaque_provenance_claim_carries_no_principal_tenant_or_subject() -> None:
    async def main() -> Drill:
        p = Partner()
        p.answer = Answer("task", task("task-1", "TASK_STATE_COMPLETED", "paid"))
        d = await drill(p, [use(message="ask"), text("ok")])
        await drive(d.rt)
        return d

    d = asyncio.run(main())
    body = d.partner.sends()[0].body or ""
    assert PROVENANCE in body
    assert '"hops": 1' in body or '"hops":1' in body
    for secret in ("acme", "alice", "api"):
        assert secret not in body
