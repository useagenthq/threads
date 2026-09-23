"""Handoffs and teams through agent.run.

A handoff records `handoff` and ends the turn `handoff`; the target thread has its own pinned
line 0, keeps the originating principal, and gets the transcript as untrusted reference; the old
thread takes no input. Team state lives only in the lead's log, written by the lead's writer."""

import asyncio
from collections.abc import Sequence

from pydantic import JsonValue

from threads import Completed, Failed, HandedOff, Thread, agent, scripted_model, sqlite
from threads.agents.bindings import capped
from threads.log import (
    Event,
    HandoffEvent,
    InjectedEvent,
    Permissions,
    Principal,
    TeamMessageEvent,
    TeamTaskClaimedEvent,
    TeamTaskCreatedEvent,
    TeamTaskUpdatedEvent,
    ThreadStartedEvent,
    ToolCallData,
    ToolResultEvent,
    ToolSpec,
    UserInputEvent,
)
from threads.reduce import Fold
from threads.result import Ok
from threads.thread.handle import open_thread

USAGE: JsonValue = {"input_tokens": 10, "output_tokens": 2}
ALICE = Principal(issuer="slack:T1", tenant="acme", subject="alice")


def text(reply: str) -> JsonValue:
    return {"content": [{"type": "text", "text": reply}], "stop_reason": "end_turn", "usage": USAGE}


def call(name: str, args: JsonValue, call_id: str) -> JsonValue:
    part: JsonValue = {"type": "tool_use", "call_id": call_id, "name": name, "input": args}
    return {"content": [part], "stop_reason": "tool_use", "usage": USAGE}


async def events_of(thread: Thread) -> list[Event]:
    timeline = await thread.timeline()
    assert isinstance(timeline, Ok)
    return [e.event for e in timeline.value.entries]


def only[T](events: Sequence[Event], kind: type[T]) -> list[T]:
    return [e for e in events if isinstance(e, kind)]


def test_a_handoff_moves_the_conversation_to_a_pinned_target_thread() -> None:
    async def main() -> None:
        billing = agent(
            name="billing",
            instructions="You handle refunds.",
            model=scripted_model({"responses": [text("Refund issued.")]}),
        )
        front = agent(
            model=scripted_model({"responses": [call("handoff", {"agent": "billing"}, "c1")]}),
            handoffs=[billing],
        )
        store = sqlite(":memory:")
        result = await front.run("I want a refund", store=store, principal=ALICE)
        assert isinstance(result, HandedOff)
        source = await events_of(result.thread)
        moved = only(source, HandoffEvent)[0]
        assert moved.data.to_thread_id == result.to_thread.id
        assert [e.type for e in source[-3:]] == ["handoff", "tool_result", "turn_completed"]
        pinned = only(source, ThreadStartedEvent)[0].data.model_dump(mode="json")
        assert pinned["policy"]["handoffs"] == ["billing"]
        target = await events_of(result.to_thread)
        started = only(target, ThreadStartedEvent)[0].data
        assert started.instructions == "You handle refunds."
        link = started.model_dump(mode="json")["parent"]
        assert (link["relation"], link["event_id"]) == ("handoff", moved.event_id)
        forwarded = only(target, InjectedEvent)[0].data
        assert (forwarded.source, forwarded.trust) == ("handoff", "untrusted_reference")
        request = only(target, UserInputEvent)[0]
        assert (request.data.source, request.data.text) == ("handoff", "I want a refund")
        assert request.actor.principal == ALICE
        assert target[-1].type == "turn_completed"

        # The old thread takes no new input: the run says where the conversation went.
        again = await front.run("hello?", store=store, thread=result.thread)
        assert isinstance(again, Failed)
        assert again.error.code == "branch_not_runnable"
        assert len(only(await events_of(result.thread), UserInputEvent)) == 1

    asyncio.run(main())


def test_a_handoff_to_an_unlisted_agent_fails_before_any_effect() -> None:
    async def main() -> None:
        billing = agent(name="billing", model=scripted_model({"responses": []}))
        front = agent(
            model=scripted_model(
                {"responses": [call("handoff", {"agent": "sales"}, "c1"), text("sorry")]}
            ),
            handoffs=[billing],
        )
        result = await front.run("buy", store=sqlite(":memory:"))
        assert isinstance(result, Completed)
        events = await events_of(result.thread)
        assert not only(events, HandoffEvent)
        refused = only(events, ToolResultEvent)[0].data
        assert (refused.origin, refused.is_error) == ("not_executed", True)

    asyncio.run(main())


def _permissions(mode: str, allow: Sequence[str] = (), deny: Sequence[str] = ()) -> Permissions:
    return Permissions.model_validate(
        {
            "mode": mode,
            "allow": list(allow),
            "ask": [],
            "deny": list(deny),
            "protected_paths": [],
            "allow_bypass": mode == "bypass",
            "plan_exit_mode": "default",
        }
    )


def test_every_ceiling_caps_the_decision_and_the_strictest_wins() -> None:
    target = _permissions("bypass", allow=["bash(git push:*)"])
    host = _permissions("default", deny=["bash(git push:*)"])
    parent = _permissions("default", allow=["bash"])
    started = ThreadStartedEvent.model_validate(
        {
            "type": "thread_started",
            "type_version": 1,
            "critical": True,
            "actor": {"kind": "host"},
            "branch_id": "0192b000-0000-7000-8000-000000000001",
            "thread_id": "0192a000-0000-7000-8000-000000000001",
            "event_id": "0192e000-0000-7000-8000-000000000001",
            "epoch": 1,
            "seq": 1,
            "time": 1,
            "prev_hash": "0" * 64,
            "data": {
                "agent_name": "t",
                "instructions": "",
                "model": {"provider": "scripted", "name": "m"},
                "model_params": {},
                "adapter": {"name": "scripted", "version": "1", "settings": {}},
                "tools": [],
                "config_hash": "0" * 64,
                "policy": {"permissions": target.model_dump(mode="json")},
            },
        }
    )
    fold = Fold(now=1, started=started.data, mode="bypass")
    bash = ToolSpec.model_validate(
        {"name": "bash", "description": "", "input_schema": {}, "effect_class": "unguarded"}
    )

    def decide(command: str, *ceilings: Permissions) -> str:
        data = ToolCallData.model_validate(
            {
                "call_id": "c",
                "name": "bash",
                "input": {"command": command},
                "request_event_id": "0192e000-0000-7000-8000-000000000002",
            }
        )
        return capped(ceilings)(fold, data, bash).decision

    assert decide("git push origin main") == "allow"
    assert decide("git push origin main", parent) == "allow"
    assert decide("git push origin main", parent, host) == "deny"
    assert decide("curl https://x.test", host) == "ask"


def test_a_team_shares_one_task_list_and_mailbox_in_the_lead_log() -> None:
    async def main() -> None:
        alice = agent(
            name="alice",
            model=scripted_model(
                {
                    "responses": [
                        call("team_task_claim", {"task_id": "lead/l2"}, "a1"),
                        call("team_task_claim", {"task_id": "lead/l1"}, "a2"),
                        call("send_message", {"to": "*", "text": "Schema is up."}, "a3"),
                        call(
                            "team_task_update", {"task_id": "lead/l1", "status": "completed"}, "a4"
                        ),
                        text("schema done"),
                    ]
                }
            ),
        )
        lead = agent(
            name="lead",
            model=scripted_model(
                {
                    "responses": [
                        call("team_task_create", {"subject": "Write the schema"}, "l1"),
                        call(
                            "team_task_create",
                            {"subject": "Write the runner", "blocked_by": ["lead/l1"]},
                            "l2",
                        ),
                        call("spawn_agent", {"agent": "alice", "prompt": "Take t1."}, "l3"),
                        text("t1 is done"),
                    ]
                }
            ),
            subagents=[alice],
        )
        store = sqlite(":memory:")
        result = await lead.run("Build it.", store=store)
        assert isinstance(result, Completed)
        events = await events_of(result.thread)
        assert [e.data.task_id for e in only(events, TeamTaskCreatedEvent)] == [
            "lead/l1",
            "lead/l2",
        ]
        assert [(e.data.task_id, e.data.member) for e in only(events, TeamTaskClaimedEvent)] == [
            ("lead/l1", "alice")
        ]
        assert [(e.data.task_id, e.data.status) for e in only(events, TeamTaskUpdatedEvent)] == [
            ("lead/l1", "completed")
        ]
        message = only(events, TeamMessageEvent)[0].data
        assert (message.from_, message.to, message.text) == ("alice", "*", "Schema is up.")
        assert message.message_id == "alice/a3"
        delivered = [e for e in only(events, InjectedEvent) if e.data.source == "agent"]
        assert [d.data.origin.id for d in delivered] == [message.message_id]
        spawned = next(e for e in events if e.type == "agent_spawned")
        child = await open_thread(store, spawned.data.model_dump()["child_thread_id"])
        assert isinstance(child, Ok)
        inside = await events_of(child.value)
        answers = [(r.data.preview, r.data.is_error) for r in only(inside, ToolResultEvent)]
        assert answers[0][1] is True
        assert answers[0][0].startswith("can't claim: ")
        assert [a[1] for a in answers[1:]] == [False, False, False]
        assert not any(e.type.startswith("team_task") for e in inside)

    asyncio.run(main())
