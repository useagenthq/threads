"""The whole path an app writes, not just the runner underneath it.

`agent(tools=[*r.tools(...)])` pins the two specs, asks for approval because a send to a partner is
not read-only, routes the send to its own runner, and records the exchange in the thread's own log.
Without this the tool works only when a test wires it up by hand.

This mirrors typescript/packages/a2a/test/outbound/agent.test.ts."""

import asyncio

from outbound_kit import CARD_URL, Answer, Partner, task, text, use

from threadsai import Completed, Parked, agent, scripted_model, sqlite
from threadsai.a2a.remote import remote
from threadsai.agents.run import execute
from threadsai.log import (
    EffectCommitEvent,
    Event,
    Principal,
    RemoteCardEvent,
    ThreadStartedEvent,
    ToolResultEvent,
)
from threadsai.result import Ok

OPERATOR = Principal(issuer="api", tenant="local", subject="operator")


def test_a_remotes_tools_spread_into_agent_tools_ask_first_and_record_their_exchange() -> None:
    async def main() -> tuple[object, list[Event], Partner]:
        p = Partner()
        p.answer = Answer("task", task("task-42", "TASK_STATE_COMPLETED", "refund 42 was paid"))
        refunds = remote("refunds", CARD_URL, timeout_ms=1, transport=p, resolve=p.resolve)
        bot = agent(
            instructions="Ask the partner.",
            model=scripted_model(
                {
                    "responses": [
                        use("refund_desk", message="did refund 42 go through?"),
                        text("It was paid."),
                    ]
                }
            ),
            tools=[
                *refunds.tools(
                    name="refund_desk",
                    description="Ask the partner's refunds desk one question.",
                )
            ],
        )
        store = sqlite(":memory:")
        parked = await bot.run("check refund 42", store=store, deps=None)
        # A send to a partner is not read_only, so the default permissions ask, and nothing is sent.
        assert isinstance(parked, Parked), parked
        assert parked.reason == "awaiting_approval"
        assert p.sends() == []
        pending = await parked.thread.pending_approvals()
        assert isinstance(pending, Ok)
        (challenge,) = pending.value
        granted = await parked.thread.approve(challenge.challenge_id, OPERATOR)
        assert isinstance(granted, Ok)
        # Resumed, not re-asked: the approval is what the turn was waiting for.
        done = await execute(bot.definition, None, {"thread": parked.thread}, None, _drop)
        timeline = await parked.thread.timeline()
        assert isinstance(timeline, Ok)
        return done, [e.event for e in timeline.value.entries], p

    done, log, p = asyncio.run(main())
    assert isinstance(done, Completed), done
    started = next(e for e in log if isinstance(e, ThreadStartedEvent))
    pinned = {f"{t.name}:{t.effect_class}" for t in started.data.tools}
    assert "refund_desk:reconcilable" in pinned
    assert "refund_desk_status:read_only" in pinned
    kinds = [e.type for e in log]
    at = kinds.index("remote_card")
    # The card and the call are durable before the begin, and the commit holds the peer's receipt.
    assert kinds[at : at + 3] == ["remote_card", "remote_call", "effect_begin"]
    assert isinstance(log[at], RemoteCardEvent)
    commit = [e for e in log if isinstance(e, EffectCommitEvent)][-1]
    assert commit.data.provider_receipt == "task-42"
    assert "remote_task_state" in kinds
    # What the model was shown is the partner's own answer, as a tool result.
    result = [e for e in log if isinstance(e, ToolResultEvent)][-1]
    assert result.data.is_error is False
    assert "refund 42 was paid" in result.data.preview
    assert len(p.sends()) == 1


def _drop(_item: object) -> None:
    pass
