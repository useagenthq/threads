"""One judge run (spec lane 22, C.2-C.4 and lane 32, D). The instructions and the transcript differ
between a single graded turn and a simulated conversation; everything else -- the budget, the strict
verdict rule and the failure-as-a-value shape -- is the same."""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from pydantic import JsonValue

from threads._generated.eval_v1 import Verdicts
from threads.agents.factory import agent
from threads.agents.results import Completed, Thread
from threads.evals.judge import Graded, judge_input, verdicts
from threads.evals.live_env import Blocked, Env, Misconfigured, run_agent
from threads.evals.tree_totals import tree_totals
from threads.log import Event


@dataclass(frozen=True, slots=True)
class JudgeAsk:
    instructions: str
    task: str
    events: Sequence[Event]
    answer: JsonValue
    rubric: Sequence[str]
    prefix_turns: int | None = None
    goal: str | None = None


@dataclass(frozen=True, slots=True)
class Judged:
    kind: Literal["graded", "error", "blocked"]
    graded: tuple[Graded, ...] = ()
    thread: Thread | None = None
    calls: int = 0
    reason: str = ""
    """kind blocked: the model the guard stopped."""


async def run_judge(ask: JudgeAsk, env: Env) -> Judged:
    judge = agent(
        name="judge",
        model=env.live.judge,
        instructions=ask.instructions,
        output=Verdicts,
        output_retries=1,
    )
    text = judge_input(ask.task, ask.events, ask.answer, ask.rubric, ask.prefix_turns, ask.goal)
    ran = await run_agent(judge.definition, text, env, None)
    if isinstance(ran, Blocked):
        return Judged("blocked", reason=ran.model)
    if isinstance(ran, Misconfigured):
        return Judged("error", reason=ran.reason)
    calls = (await tree_totals(env.store, ran.thread.branch)).requests
    if ran.status == "budget_exhausted":
        return Judged("error", thread=ran.thread, calls=calls, reason="budget_exhausted")
    parsed: JsonValue = json.loads(ran.output) if isinstance(ran, Completed) else None
    graded = verdicts(parsed, ask.rubric) if isinstance(ran, Completed) else None
    if graded is None:
        return Judged("error", thread=ran.thread, calls=calls, reason="judge_invalid")
    return Judged("graded", graded, ran.thread, calls)
