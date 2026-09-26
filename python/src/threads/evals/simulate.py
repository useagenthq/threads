"""A simulated case's live conversation (spec lane 32, B): the real turns before the saved one are
the prefix -- continued when the agent is unchanged, re-driven when it is not -- and the saved
turn's text is the opener the simulation starts from. Every effectful call answers from the
recorded stubs on every turn, and one budget covers the whole conversation."""

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final, Literal

from pydantic import JsonValue

from threads._generated.eval_v1 import CaseSimulate, CaseSimulate1
from threads.agents.results import Completed, RunResult, Thread
from threads.evals.case_dir import CaseDir
from threads.evals.checks import CaseLog
from threads.evals.judge import JUDGE_CONVERSATION_V1, judge_items
from threads.evals.judge_run import JudgeAsk, Judged, run_judge
from threads.evals.ledger import Ledger, spent_so_far, start_ledger
from threads.evals.live_env import (
    Blocked,
    Env,
    EvalAgent,
    Misconfigured,
    Outcome,
    Simulation,
    answer_of,
    cost_sum,
    events_of,
    new_thread,
    run_agent,
    unfinished,
)
from threads.evals.remaining_budget import remaining_budget
from threads.evals.simulate_prefix import Plan, plan_prefix, prefix_texts
from threads.evals.simulated_user import VisibleMessage, visible_conversation
from threads.evals.simulated_user_run import Play, UserPlayer, player_for
from threads.evals.stub_queue import StubScope, stub_gateway
from threads.evals.tree_totals import tree_totals
from threads.log import Budget
from threads.loop.stubs import StubGateway

DEFAULT_MAX_MESSAGES: Final = 5


@dataclass(frozen=True, slots=True)
class Ended:
    kind: Literal["ok", "error", "blocked"]
    ending: Literal["user_done", "script_done", "max_messages"] = "user_done"
    reason: str = ""


@dataclass(slots=True)
class Running:
    thread: Thread | None
    lead: Completed[str] | None = None
    messages: int = 1
    ledger: Ledger = field(default_factory=lambda: start_ledger(0, 0))


@dataclass(frozen=True, slots=True)
class Loop:
    target: EvalAgent
    plan: Plan
    player: UserPlayer
    stubs: StubGateway
    max_messages: int
    env: Env


async def _left(loop: Loop, state: Running) -> Budget | None:
    spent = await spent_so_far(state.ledger, state.thread, loop.player.thread)
    return remaining_budget(loop.env.live.budget, spent)


def _after(loop: Loop, state: Running, ran: RunResult[str]) -> Ended | None:
    """What the run leaves the conversation in: an unrecorded effect ends it closed."""
    state.thread = ran.thread
    missed = loop.stubs.first_unmatched
    if missed is not None:
        return Ended("error", reason=f"unmatched_external_op: {missed}")
    if not isinstance(ran, Completed):
        return Ended("error", reason=unfinished(ran))
    state.lead = ran
    return None


async def _send(loop: Loop, state: Running, text: str) -> Ended | None:
    """One agent run under what is left of the conversation budget."""
    budget = await _left(loop, state)
    if budget is None:
        return Ended("error", reason="budget_exhausted")
    on = state.thread or await new_thread(loop.env)
    ran = await run_agent(loop.target.definition, text, loop.env, on, budget, stubs=loop.stubs)
    if isinstance(ran, Blocked):
        return Ended("blocked", reason=ran.model)
    if isinstance(ran, Misconfigured):
        return Ended("error", reason=ran.reason)
    return _after(loop, state, ran)


def _stopped(play: Play) -> Ended:
    """Whatever the player said that was not a message, as the conversation's ending."""
    if play.kind == "blocked":
        return Ended("blocked", reason=play.text)
    if play.kind == "ended":
        return Ended("ok", ending=play.ending)
    return Ended("error", reason=play.reason)


async def _round(
    loop: Loop, state: Running, seen: Sequence[VisibleMessage]
) -> Ended | tuple[VisibleMessage, ...]:
    """One round: what the user says next, then the agent's answer to it."""
    budget = await _left(loop, state)
    if budget is None:
        return Ended("error", reason="budget_exhausted")
    play = await loop.player.next(seen, budget)
    if play.kind != "message":
        return _stopped(play)
    if state.messages >= loop.max_messages:
        return Ended("ok", ending="max_messages")
    state.messages += 1
    stopped = await _send(loop, state, play.text)
    if stopped is not None:
        return stopped
    # The agent's reply as the user sees it: its text, or its canonical JSON output.
    reply = "" if state.lead is None else state.lead.output
    return (VisibleMessage("agent", reply),)


async def _converse(loop: Loop, opener: str, state: Running) -> Ended:
    """B.2: re-drive the prefix, send the opener, then alternate until someone stops."""
    for text in loop.plan.redrive:
        stopped = await _send(loop, state, text)
        if stopped is not None:
            return stopped
    opened = await _send(loop, state, opener)
    if opened is not None:
        return opened
    # The simulator's first input is the whole visible conversation, read off the log.
    seen: tuple[VisibleMessage, ...] = ()
    if state.thread is not None:
        seen = visible_conversation(await events_of(loop.env.store, state.thread.branch))
    while True:
        got = await _round(loop, state, seen)
        if isinstance(got, Ended):
            return got
        seen = got


def _max_messages(simulate: CaseSimulate) -> int:
    if isinstance(simulate, CaseSimulate1):
        given = simulate.max_messages
        return given if isinstance(given, int) else DEFAULT_MAX_MESSAGES
    return len(simulate.messages) + 1


def _goal(simulate: CaseSimulate) -> str | None:
    return simulate.goal if isinstance(simulate, CaseSimulate1) else None


async def _totals(loop: Loop, state: Running) -> tuple[int, int, Sequence[Thread]]:
    """The conversation's agent and user model calls, and the threads its cost is read from."""
    user = loop.player.thread
    agent_calls = 0
    if state.thread is not None:
        totals = await tree_totals(loop.env.store, state.thread.branch, loop.plan.since)
        agent_calls = totals.requests
    threads = [t for t in (state.thread, user) if t is not None]
    return agent_calls, await loop.player.calls(), threads


def _check(judged: Judged, answer: JsonValue, items: int, loop: Loop, thread: Thread) -> JsonValue:
    passed = sum(1 for g in judged.graded if g.passed)
    check: dict[str, JsonValue] = {
        "answer": answer,
        "transcript_items": items,
        "score": passed / len(judged.graded),
        "fresh_sandbox": loop.env.fresh,
        "verdicts": [g.to_json() for g in judged.graded],
    }
    if loop.env.kept and judged.thread is not None:
        check |= {"thread_id": thread.id, "judge_thread_id": judged.thread.id}
    return check


@dataclass(frozen=True, slots=True)
class Grading:
    """What grading a finished conversation needs besides the loop and its state."""

    opener: str
    criteria: Sequence[str]
    simulate: CaseSimulate
    simulation: Simulation


async def _grade(loop: Loop, state: Running, g: Grading) -> Outcome:
    """Grades the whole conversation: the opener is the task, the rest is the transcript."""
    thread = state.thread
    agent_calls, user_calls, threads = await _totals(loop, state)
    if thread is None:
        return Outcome("error", reason="no_run", agent_calls=agent_calls, user_calls=user_calls)
    events = await events_of(loop.env.store, thread.branch)
    answer = None if state.lead is None else answer_of(loop.target.definition, state.lead.output)
    items = len(judge_items(events, loop.plan.turns))
    ask = JudgeAsk(
        JUDGE_CONVERSATION_V1,
        g.opener,
        events,
        answer,
        g.criteria,
        loop.plan.turns,
        _goal(g.simulate),
    )
    judged = await run_judge(ask, loop.env)
    if judged.kind == "blocked":
        return Outcome("blocked", reason=judged.reason)
    graded_threads = [*threads, *([judged.thread] if judged.thread is not None else [])]
    cost = await cost_sum(graded_threads)
    check = None if judged.kind == "error" else _check(judged, answer, items, loop, thread)
    return Outcome(
        "error" if judged.kind == "error" else "graded",
        check,
        reason=judged.reason if judged.kind == "error" else "",
        agent_calls=agent_calls,
        user_calls=user_calls,
        judge_calls=judged.calls,
        cost=cost,
        simulation=g.simulation,
    )


async def simulate_case(  # noqa: PLR0913, PLR0917 - one case, its agent and its live env
    case: CaseDir,
    log: CaseLog,
    target: EvalAgent,
    criteria: Sequence[str],
    simulate: CaseSimulate,
    env: Env,
) -> Outcome:
    """Runs a simulated case's conversation and grades it (spec lane 32, B and D)."""
    if case.meta.simulate_blocked is not None:
        return Outcome("skipped", reason=case.meta.simulate_blocked)
    opener = case.meta.input.text if case.meta.input is not None else None
    if opener is None or None in prefix_texts(log):
        return Outcome("skipped", reason="simulate_content_input")
    player = player_for(simulate, env)
    if isinstance(player, Blocked):
        return Outcome("blocked", reason=player.model)
    plan = await plan_prefix(case, log, target, env)
    scope: StubScope = "conversation" if plan.mode == "redriven" else "turn"
    script: dict[str, JsonValue] = case.stubs if isinstance(case.stubs, dict) else {"stubs": []}
    stubs = stub_gateway(script, scope)
    loop = Loop(target, plan, player, stubs, _max_messages(simulate), env)
    thread = plan.thread
    state = Running(thread, ledger=start_ledger(plan.since, plan.base_cost_nanos))
    end = await _converse(loop, opener, state)
    if end.kind == "blocked":
        return Outcome("blocked", reason=end.reason)
    if end.kind == "error":
        agent_calls, user_calls, threads = await _totals(loop, state)
        return Outcome(
            "error",
            reason=end.reason,
            agent_calls=agent_calls,
            user_calls=user_calls,
            cost=await cost_sum(threads),
        )
    user = loop.player.thread
    simulation = Simulation(
        state.messages,
        plan.mode,
        plan.turns,
        end.ending,
        user.id if env.kept and user is not None else None,
    )
    return await _grade(loop, state, Grading(opener, criteria, simulate, simulation))
