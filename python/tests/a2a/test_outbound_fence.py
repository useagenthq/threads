"""Dispatch authority at the A2A send point (invariant 2).

The loop's fence before a dispatch cannot cover what happens next: the guard resolves a name and the
transport opens a socket, and the branch can change owner in that window. So the run's own lease
fence rides on the request and is awaited again at the real write, and a request that arrives there
without one is refused rather than assumed to be authorized.

Each drill counts what the partner was asked to write, because a drill that could not say how many
times we sent would pass just as well on an empty log.

Mirrors typescript/packages/a2a/test/outbound/fence.test.ts."""

import asyncio

from client_kit import RPC, TASK, Answering, json_response, refused_fence, sending
from kit import acquire
from outbound_kit import Drill, Partner, drill

from threadsai.a2a.protocol import METHODS, NotSent, call, fetch_bytes
from threadsai.loop.drive import drive
from threadsai.loop.runtime import Failed


def _usurps(d: Drill, operation: str) -> None:
    """Another owner takes the branch while we are connected to the partner: past this writer's
    lease and at a new epoch, which is what its fence has to see."""

    async def took(method: str) -> None:
        if method != operation:
            return
        d.clock.now += 60_000
        await acquire(d.store, d.branch, "usurper", d.clock)

    d.partner.on_connect = took


def test_a_send_whose_lease_is_taken_after_connect_writes_nothing() -> None:
    async def main() -> tuple[Drill, object]:
        d = await drill(Partner())
        _usurps(d, "SendMessage")
        return d, await drive(d.rt)

    d, halt = asyncio.run(main())
    # The card was read and the attempt is durable; then not one byte of the send left.
    assert [r.method for r in d.partner.requests] == ["card"]
    assert d.partner.sends() == []
    assert "remote_call" in d.kinds()
    assert isinstance(halt, Failed)
    assert halt.code == "branch_busy"


def test_a_card_read_whose_lease_is_taken_after_connect_writes_nothing() -> None:
    """The card read is on the send's own path, so it is fenced too: a run that lost the branch
    while resolving the partner does not read its card and never begins the attempt."""

    async def main() -> tuple[Drill, object]:
        d = await drill(Partner())
        _usurps(d, "card")
        return d, await drive(d.rt)

    d, halt = asyncio.run(main())
    assert d.partner.requests == []
    assert "remote_call" not in d.kinds()
    assert isinstance(halt, Failed)
    assert halt.code == "branch_busy"


def test_no_operation_writes_a_byte_under_a_refused_fence() -> None:
    """Every operation goes out through one call, so one refusal covers send, status, follow,
    reconcile and the card read alike."""

    async def main() -> None:
        for method in METHODS:
            transport = Answering(json_response({"task": TASK}))
            answer = await call(RPC, method, {}, sending(transport, fence=refused_fence))
            assert isinstance(answer, NotSent), (method, answer)
            assert transport.sent == [], method
        card = Answering(json_response({}))
        got = await fetch_bytes(
            "https://partner.example/card.json", 4096, sending(card, fence=refused_fence)
        )
        assert got.bytes_ is None
        assert card.sent == []

    asyncio.run(main())
