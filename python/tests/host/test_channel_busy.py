"""A channel answer that arrives while another process holds the branch (review M3, decision 5):
the press stays durable in the inbox, is applied once the lease frees, and presses are applied in
the order they arrived."""

import asyncio

from pydantic import JsonValue
from test_channel_approvals import (
    APPROVER,
    TEAM,
    USAGE,
    ItemsChannel,
    Note,
    message,
    press,
    text,
    until,
    webhook,
)

from threads import RunContext, agent, scripted_model, sqlite, tool
from threads.agents.store import now_ms, open_store, scoped
from threads.host import host
from threads.log import BranchId
from threads.result import Ok
from threads.store.sql import text_of


def _deny(key: str, challenge: str) -> JsonValue:
    return {
        "kind": "decision",
        "principal": APPROVER.model_dump(),
        "address": "C1",
        "item_key": key,
        "challenge_id": challenge,
        "decision": "deny",
    }


def test_presses_while_the_branch_is_busy_wait_and_apply_in_order() -> None:
    runs: list[str] = []

    async def send(args: Note, _ctx: RunContext[None]) -> str:
        runs.append(args.text)
        return "sent"

    use: JsonValue = {
        "content": [{"type": "tool_use", "call_id": "c1", "name": "send", "input": {"text": "x"}}],
        "stop_reason": "tool_use",
        "usage": USAGE,
    }
    send_tool = tool(name="send", description="Send.", input=Note, runs="host", execute=send)
    script: JsonValue = {"responses": [use, text("Not sent.")]}
    bot = agent(model=scripted_model(script), tools=[send_tool], approvers=[APPROVER])
    store = sqlite(":memory:")

    async def main() -> list[str]:
        async with host(store=store, agents={"bot": bot}, channels={"fake": ItemsChannel()}) as h:
            sq = await open_store(scoped(store, TEAM))

            async def states() -> list[str]:
                rows = await sq.run(lambda c: c.execute("SELECT state FROM approvals").fetchall())
                return [text_of(r[0]) for r in rows]

            async def challenge() -> tuple[str, str] | None:
                rows = await sq.run(
                    lambda c: c.execute("SELECT challenge_id, branch_id FROM approvals").fetchall()
                )
                return (text_of(rows[0][0]), text_of(rows[0][1])) if rows else None

            async def parked() -> bool | None:
                acquired = await sq.acquire(BranchId(branch), "probe", now_ms)
                if not isinstance(acquired, Ok):
                    return None
                await acquired.value.release()
                return True

            await h.receive("fake", webhook("d1", message("m1", "send it")))
            cid, branch = await until(challenge)
            await until(parked)
            other = await sq.acquire(BranchId(branch), "other-process", now_ms)
            assert isinstance(other, Ok)
            await h.receive("fake", webhook("d2", _deny("b1", cid)))
            await h.receive("fake", webhook("d3", press("b2", cid, APPROVER)))
            await asyncio.sleep(0.2)
            assert await states() == ["open"]
            await other.value.release()

            async def answered() -> list[str] | None:
                now = await states()
                return None if now == ["open"] else now

            return await until(answered)

    assert asyncio.run(main()) == ["denied"]
    assert runs == []
