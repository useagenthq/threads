"""The live check (spec lane 22, C): the current agent answers the case input on a new thread, with
every mediated call answered from the recorded stubs (an eval never performs a real side effect),
and a judge model grades the whole turn against the rubric. A case with `simulate` goes to the
multi-turn conversation instead (spec lane 32, B)."""

from collections.abc import Sequence

from pydantic import JsonValue

from threads.agents.results import Completed, Thread
from threads.evals.case_dir import CaseDir
from threads.evals.checks import CaseLog
from threads.evals.judge import JUDGE_V1, Graded, transcript
from threads.evals.judge_run import JudgeAsk, run_judge
from threads.evals.live_env import (
    EVAL_PRINCIPAL,
    Blocked,
    Env,
    EvalAgent,
    Live,
    Misconfigured,
    Outcome,
    answer_of,
    cost_sum,
    events_of,
    new_thread,
    run_agent,
    unfinished,
)
from threads.evals.simulate import simulate_case
from threads.evals.stub_queue import stub_queue
from threads.evals.tree_totals import tree_totals
from threads.log import Event

__all__ = ["EVAL_PRINCIPAL", "Env", "EvalAgent", "Live", "Outcome", "live_check"]


def _check(
    graded: Sequence[Graded], answer: JsonValue, events: Sequence[Event], env: Env
) -> dict[str, JsonValue]:
    passed = sum(1 for g in graded if g.passed)
    return {
        "answer": answer,
        "transcript_items": len(transcript(events)),
        "score": passed / len(graded),
        "fresh_sandbox": env.fresh,
        "verdicts": [g.to_json() for g in graded],
    }


async def _judged(
    case: CaseDir, target: EvalAgent, lead: Completed[str], criteria: Sequence[str], env: Env
) -> Outcome:
    events = await events_of(env.store, lead.thread.branch)
    answer = answer_of(target.definition, lead.output)
    text = case.meta.input.text if case.meta.input is not None else None
    ask = JudgeAsk(JUDGE_V1, text or "", events, answer, criteria)
    ran = await run_judge(ask, env)
    agent_calls = (await tree_totals(env.store, lead.thread.branch)).requests
    if ran.kind == "blocked":
        return Outcome("blocked", reason=ran.reason)
    threads: list[Thread] = [lead.thread]
    if ran.thread is not None:
        threads.append(ran.thread)
    cost = await cost_sum(threads)
    if ran.kind == "error":
        return Outcome(
            "error",
            reason=ran.reason,
            agent_calls=agent_calls,
            judge_calls=ran.calls,
            cost=cost,
        )
    check = _check(ran.graded, answer, events, env)
    if env.kept and ran.thread is not None:
        check |= {"thread_id": lead.thread.id, "judge_thread_id": ran.thread.id}
    return Outcome("graded", check, agent_calls=agent_calls, judge_calls=ran.calls, cost=cost)


async def _one_turn(case: CaseDir, target: EvalAgent, text: str, env: Env) -> Outcome:
    """Lane 22's single graded turn: one run on a new thread, then the judge."""
    criteria = [*(case.meta.rubric or ()), *env.live.rubric]
    script: dict[str, JsonValue] = case.stubs if isinstance(case.stubs, dict) else {"stubs": []}
    thread = await new_thread(env)
    lead = await run_agent(target.definition, text, env, thread, stubs=stub_queue(script, "turn"))
    if isinstance(lead, Blocked):
        return Outcome("blocked", reason=lead.model)
    if isinstance(lead, Misconfigured):
        return Outcome("error", reason=lead.reason)
    if not isinstance(lead, Completed):
        calls = (await tree_totals(env.store, lead.thread.branch)).requests
        return Outcome(
            "error",
            reason=unfinished(lead),
            agent_calls=calls,
            cost=await cost_sum([lead.thread]),
        )
    return await _judged(case, target, lead, criteria, env)


async def live_check(case: CaseDir, log: CaseLog, target: EvalAgent, env: Env) -> Outcome:
    """Runs the case input on the current agent and grades it."""
    criteria = [*(case.meta.rubric or ()), *env.live.rubric]
    if not criteria:
        return Outcome("skipped", reason="no_rubric")
    if case.meta.simulate is not None:
        return await simulate_case(case, log, target, criteria, case.meta.simulate, env)
    text = case.meta.input.text if case.meta.input is not None else None
    if text is None:
        return Outcome("skipped", reason="offline_not_runnable:content_input")
    return await _one_turn(case, target, text, env)
