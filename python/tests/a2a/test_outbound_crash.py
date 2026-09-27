"""The crash drills of lane 30's uncertainty contract, on the outbound send.

Each kills the process at a different point and then asserts, FROM THE LOG, what the next process
does. An uncertain outcome is never a success and never a silent re-send: it parks (invariant 3).

Every drill also counts the partner's SendMessage requests, because a drill that could not say how
many times we sent would pass just as well on an empty log.

This mirrors typescript/packages/a2a/test/outbound/crash.test.ts case for case."""

import asyncio
from dataclasses import replace

import pytest
from outbound_kit import (
    TOOL,
    Answer,
    Crash,
    Drill,
    Partner,
    ask_all,
    cancelled,
    drill,
    message_id_in,
    task,
    tools_changed,
    use,
)

from threads.log import (
    EffectCommitEvent,
    EffectResolvedEvent,
    EffectUnknownEvent,
    ParkedEvent,
    RemoteCallEvent,
    ToolResultEvent,
)
from threads.loop.drive import drive
from threads.loop.recovery import recover
from threads.loop.runtime import Parked
from threads.result import Ok


def _lists(d: Drill) -> int:
    return [r.method for r in d.partner.requests].count("ListTasks")


def test_killed_after_effect_begin_before_the_request_left() -> None:
    async def main() -> tuple[Drill, object]:
        d = await drill(Partner())
        d.runner.crash_before_send = True
        with pytest.raises(Crash):
            await drive(d.rt)
        # The card and the call are durable in the begin's own append (rules 56 and 58).
        assert d.kinds()[-3:] == ["remote_card", "remote_call", "effect_begin"]
        assert d.partner.sends() == []
        rt = await d.restart()
        d.runner.crash_before_send = False
        return d, await recover(rt)

    d, halt = asyncio.run(main())
    unknown = d.last("effect_unknown")
    assert isinstance(unknown, EffectUnknownEvent)
    assert unknown.data.reason == "crash_after_begin"
    parked = d.last("parked")
    assert isinstance(parked, ParkedEvent)
    assert parked.data.reason == "effect_unknown"
    # Reconciliation looked; a not_found never proves absence, so it parks rather than re-sends.
    assert _lists(d) == 1
    assert "effect_resolved" not in d.kinds()
    assert "tool_result" not in d.kinds()
    assert isinstance(halt, Parked)
    # The whole point: no byte ever left, and the log is what says so.
    assert d.partner.sends() == []


def test_the_answer_is_lost_and_the_peer_hides_the_task() -> None:
    async def main() -> tuple[Drill, object]:
        p = Partner(hidden=True)
        p.answer = Answer("lost", task("task-7", "TASK_STATE_WORKING"), "ECONNRESET")
        d = await drill(p)
        parked = await drive(d.rt)
        assert isinstance(parked, Parked)
        assert "effect_commit" not in d.kinds()
        assert "effect_resolved" not in d.kinds()
        assert "tool_result" not in d.kinds()
        assert len(p.sends()) == 1
        # The peer lists it now, with our own messageId in its history.
        held = p.tasks["task-7"]
        p.hidden = False
        p.tasks["task-7"] = {
            **task("task-7", "TASK_STATE_COMPLETED", "refund 42 was paid"),
            "history": held["history"],
        }
        rt = await d.restart()
        return d, await recover(rt)

    d, _halt = asyncio.run(main())
    unknown = d.last("effect_unknown")
    assert isinstance(unknown, EffectUnknownEvent)
    # A reset after dispatch is uncertainty, never a failed result.
    assert unknown.data.reason == "transport_error"
    resolved = d.last("effect_resolved")
    assert isinstance(resolved, EffectResolvedEvent)
    assert (resolved.data.outcome, resolved.data.by) == ("confirmed_success", "reconcile")
    result = d.last("tool_result")
    assert isinstance(result, ToolResultEvent)
    assert result.data.is_error is False
    assert "refund 42 was paid" in result.data.preview
    # Settled by the peer's own receipt, with nothing sent a second time.
    assert len(d.partner.sends()) == 1


def test_killed_after_the_answer_arrived_before_it_was_recorded() -> None:
    async def main() -> tuple[Drill, object]:
        p = Partner(crash_after_answer=True)
        p.answer = Answer("task", task("task-3", "TASK_STATE_COMPLETED", "refund 42 was paid"))
        d = await drill(p)
        with pytest.raises(Crash):
            await drive(d.rt)
        assert len(p.sends()) == 1
        assert "effect_commit" not in d.kinds()
        assert "tool_result" not in d.kinds()
        p.crash_after_answer = False
        rt = await d.restart()
        return d, await recover(rt)

    d, _halt = asyncio.run(main())
    unknown = d.last("effect_unknown")
    assert isinstance(unknown, EffectUnknownEvent)
    assert unknown.data.reason == "crash_after_begin"
    resolved = d.last("effect_resolved")
    assert isinstance(resolved, EffectResolvedEvent)
    assert (resolved.data.outcome, resolved.data.by) == ("confirmed_success", "reconcile")
    result = d.last("tool_result")
    assert isinstance(result, ToolResultEvent)
    assert "refund 42 was paid" in result.data.preview
    assert len(d.partner.sends()) == 1


def test_the_commit_what_it_observed_and_the_result_are_one_append() -> None:
    async def main() -> Drill:
        p = Partner()
        p.answer = Answer("task", task("task-5", "TASK_STATE_COMPLETED", "already paid"))
        d = await drill(p)
        await drive(d.rt)
        return d

    d = asyncio.run(main())
    log = d.kinds()
    at = log.index("effect_commit")
    # One transaction, in this order: a state we observed follows the receipt it was read against
    # (rule 57), so a crash can never leave a commit whose result is lost.
    assert log[at : at + 3] == ["effect_commit", "remote_task_state", "tool_result"]
    commit = d.last("effect_commit")
    assert isinstance(commit, EffectCommitEvent)
    assert commit.data.provider_receipt == "task-5"
    result = d.last("tool_result")
    assert isinstance(result, ToolResultEvent)
    assert "already paid" in result.data.preview
    assert len(d.partner.sends()) == 1


def test_a_peer_that_answers_slowly_is_working_and_is_not_sent_to_again() -> None:
    async def main() -> Drill:
        p = Partner()
        # The peer created the task and is still working on it when the deadline passes.
        p.answer = Answer("task", task("task-17", "TASK_STATE_WORKING"))
        d = await drill(p)
        await drive(d.rt)
        return d

    d = asyncio.run(main())
    # Slow is not uncertain: the commit already happened, so we hold the peer's receipt.
    commit = d.last("effect_commit")
    assert isinstance(commit, EffectCommitEvent)
    assert commit.data.provider_receipt == "task-17"
    result = d.last("tool_result")
    assert isinstance(result, ToolResultEvent)
    assert result.data.is_error is False
    assert '"status":"working"' in result.data.preview
    assert "task-17" in result.data.preview
    assert "effect_unknown" not in d.kinds()
    assert "parked" not in d.kinds()
    # The model checks later with the status tool; nothing is re-sent.
    assert len(d.partner.sends()) == 1


def test_a_refused_connection_proves_nothing_left() -> None:
    async def main() -> Drill:
        p = Partner()
        p.answer = Answer("refused", None, "ECONNREFUSED")
        d = await drill(p)
        await drive(d.rt)
        return d

    d = asyncio.run(main())
    resolved = d.last("effect_resolved")
    assert isinstance(resolved, EffectResolvedEvent)
    assert (resolved.data.outcome, resolved.data.by) == ("not_sent", "adapter")
    assert "parked" not in d.kinds()
    # One remote_call per call id, whatever happens to its attempts (rule 58).
    assert d.kinds().count("remote_call") == 1


def test_a_park_reconciliation_cannot_settle_escalates_once() -> None:
    async def main() -> Drill:
        p = Partner(hidden=True)
        p.answer = Answer("lost", task("task-13", "TASK_STATE_WORKING"), "ECONNRESET")
        d = await drill(p)
        await drive(d.rt)
        assert "parked" in d.kinds()
        assert "park_escalated" not in d.kinds()
        # A try before the floor still only parks: escalating early calls a person for nothing.
        await recover(await d.restart(60_000))
        assert "park_escalated" not in d.kinds()
        # Past the floor, the next try that cannot settle it calls a person.
        await recover(await d.restart(300_000))
        assert "park_escalated" in d.kinds()
        assert "effect_resolved" not in d.kinds()
        # A second try does not call again: one escalation per park.
        await recover(await d.restart(300_000))
        return d

    d = asyncio.run(main())
    assert d.kinds().count("park_escalated") == 1
    assert len(d.partner.sends()) == 1


def test_recovery_rechecks_cancellation_before_dispatching_a_call_that_never_began() -> None:
    async def main() -> Drill:
        d = await drill(Partner())
        # Killed while preparing: the call is recorded and allowed, and nothing of it began.
        d.runner.crash_in_begin = True
        with pytest.raises(Crash):
            await drive(d.rt)
        assert d.kinds()[-1] == "permission_decision"
        assert "effect_begin" not in d.kinds()
        d.runner.crash_in_begin = False
        # A cancel another process recorded while this branch was down.
        rt = await d.restart()
        assert isinstance(await rt.append(cancelled()), Ok)
        d.rt = rt
        await recover(rt)
        return d

    d = asyncio.run(main())
    assert "effect_begin" not in d.kinds()
    assert "remote_call" not in d.kinds()
    result = d.last("tool_result")
    assert isinstance(result, ToolResultEvent)
    assert result.data.is_error is True
    assert d.partner.sends() == []


def test_recovery_rechecks_approval_before_dispatching_a_call_that_never_began() -> None:
    async def main() -> Drill:
        d = await drill(Partner(), [use(message="ask")])
        d.rt = replace(d.rt, authorize=ask_all)
        halt = await drive(d.rt)
        assert isinstance(halt, Parked)
        parked = d.last("parked")
        assert isinstance(parked, ParkedEvent)
        assert parked.data.reason == "awaiting_approval"
        assert "effect_begin" not in d.kinds()
        # A restart does not read "not started" as permission: it parks on the challenge again.
        rt = replace(await d.restart(), authorize=ask_all)
        d.rt = rt
        await recover(rt)
        return d

    d = asyncio.run(main())
    assert "effect_begin" not in d.kinds()
    assert "remote_call" not in d.kinds()
    assert d.partner.sends() == []


def test_recovery_rechecks_policy_before_dispatching_a_call_that_never_began() -> None:
    async def main() -> Drill:
        d = await drill(Partner())
        d.runner.crash_in_begin = True
        with pytest.raises(Crash):
            await drive(d.rt)
        assert "effect_begin" not in d.kinds()
        d.runner.crash_in_begin = False
        # The tool leaves the set while the branch is down: a removal is a policy change, so an
        # earlier allow never dispatches past it.
        rt = await d.restart()
        assert isinstance(await rt.append(tools_changed(d.specs, TOOL)), Ok)
        d.rt = rt
        await recover(rt)
        return d

    d = asyncio.run(main())
    assert "effect_begin" not in d.kinds()
    assert "remote_call" not in d.kinds()
    result = d.last("tool_result")
    assert isinstance(result, ToolResultEvent)
    assert result.data.is_error is True
    assert d.partner.sends() == []


def test_every_attempt_of_one_call_carries_one_message_id() -> None:
    async def main() -> Drill:
        p = Partner(hidden=True)
        p.answer = Answer("lost", task("task-11", "TASK_STATE_WORKING"), "ECONNRESET")
        d = await drill(p)
        await drive(d.rt)
        return d

    d = asyncio.run(main())
    ids = {message_id_in(r.body or "") for r in d.partner.sends()}
    assert len(ids) == 1
    call = d.last("remote_call")
    assert isinstance(call, RemoteCallEvent)
    assert call.data.message_id == next(iter(ids))
