"""The live check (spec lane 22, C): the current agent answers the case input on a new thread,
with every mediated call answered from the recorded stubs (an eval never performs a real side
effect), and a judge model grades the whole turn against the rubric. Real model calls, so it runs
only when asked, under a budget."""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal, Protocol, assert_never

from pydantic import JsonValue

from threads._generated.eval_v1 import Verdicts
from threads.agents.config import ConfigError
from threads.agents.definition import Definition
from threads.agents.factory import agent
from threads.agents.results import Completed, Failed, RunResult, StreamEvent, Thread
from threads.agents.run import RunOptions, execute
from threads.agents.store import Store, now_ms, open_store
from threads.evals.case_dir import CaseDir
from threads.evals.judge import JUDGE_V1, Graded, judge_input, transcript, verdicts
from threads.evals.report import add_cost
from threads.log import (
    AgentSpawnedEvent,
    BranchId,
    Budget,
    Cost,
    Event,
    ModelRequestEvent,
    Principal,
    ThreadId,
    UnknownEvent,
)
from threads.loop.guard import ModelBlockedError
from threads.loop.model import Model
from threads.loop.stubs import Stub, parse_stubs
from threads.result import Ok
from threads.store.lines import uuid7
from threads.thread.read import read_log

EVAL_PRINCIPAL: Final = Principal(issuer="threads", tenant="evals", subject="eval-runner")


@dataclass(frozen=True, slots=True)
class Live:
    """What `live` / `--live` gives: the judge model, the budget of every run (the existing
    `Budget`), and criteria added after each case's own."""

    judge: Model
    budget: Budget
    rubric: tuple[str, ...] = ()


class EvalAgent(Protocol):
    """An agent the runner can pin and run: an `agent(...)` whose tools take no deps."""

    @property
    def name(self) -> str: ...

    @property
    def definition(self) -> Definition[None]: ...


@dataclass(frozen=True, slots=True)
class Env:
    live: Live
    store: Store
    kept: bool
    """Thread ids are reported only when the threads are kept (a store the caller passed)."""
    fresh: bool
    """The agent runs on a fresh sandbox: a live eval never restores the case's snapshot."""


@dataclass(frozen=True, slots=True)
class Outcome:
    kind: Literal["graded", "error", "skipped", "blocked"]
    check: JsonValue = None
    reason: str = ""
    agent_calls: int = 0
    judge_calls: int = 0
    cost: Cost | None = None


def _drop(_item: StreamEvent) -> None:
    pass


async def _events(store: Store, branch: BranchId) -> tuple[Event, ...]:
    read = await read_log(store, branch)
    if not isinstance(read, Ok):
        return ()
    return tuple(e for e in read.value.fold.events if not isinstance(e, UnknownEvent))


async def _requests(store: Store, branch: BranchId) -> int:
    """model_request events of a thread and its subagents, depth first."""
    events = await _events(store, branch)
    count = sum(1 for e in events if isinstance(e, ModelRequestEvent))
    sq = await open_store(store)
    for e in events:
        if isinstance(e, AgentSpawnedEvent):
            root = await sq.root(e.data.child_thread_id)
            if isinstance(root, Ok):
                count += await _requests(store, root.value)
    return count


async def _cost(threads: Sequence[Thread]) -> Cost | None:
    """Thread.cost(tree=True) summed; None once any part ran unpriced."""
    total: Cost | None = None
    for i, t in enumerate(threads):
        got = await t.cost(tree=True)
        total = add_cost(total, got.value if isinstance(got, Ok) else None, first=i == 0)
    return total


@dataclass(frozen=True, slots=True)
class Blocked:
    """The model-request guard stopped the run before dispatch."""

    model: str


@dataclass(frozen=True, slots=True)
class Misconfigured:
    """The agent's setup failed: a ConfigError, as its code and message."""

    reason: str


async def _run(
    definition: Definition[None],
    text: str,
    env: Env,
    thread: Thread,
    stubs: tuple[Stub, ...] | None = None,
) -> RunResult[str] | Blocked | Misconfigured:
    """One run: its result, a guard block, or a setup mistake. Any other throw is a bug."""
    options: RunOptions[None] = {
        "store": env.store,
        "budget": env.live.budget,
        "principal": EVAL_PRINCIPAL,
        "thread": thread,
    }
    try:
        return await execute(definition, text, options, None, _drop, stubs=stubs)
    except ModelBlockedError as blocked:
        return Blocked(blocked.model)
    except ConfigError as error:
        return Misconfigured(f"{error.code}: {error.message}")


async def _new_thread(env: Env) -> Thread:
    """A new thread for one live run."""
    sq = await open_store(env.store)
    now = now_ms()
    thread_id, branch_id = ThreadId(uuid7(now)), BranchId(uuid7(now))
    await sq.create(thread_id, branch_id, now)
    return Thread(thread_id, branch_id, env.store)


def _recorded(stubs: JsonValue) -> tuple[Stub, ...] | None:
    """The case's recorded stubs, which the run answers every mediated call from."""
    return parse_stubs(stubs) if isinstance(stubs, dict) else None


def _refused(ran: Blocked | Misconfigured, calls: int = 0, cost: Cost | None = None) -> Outcome:
    match ran:
        case Blocked(model=model):
            return Outcome("blocked", reason=model)
        case Misconfigured(reason=reason):
            return Outcome("error", reason=reason, agent_calls=calls, cost=cost)
        case _:
            assert_never(ran)


def _answer(definition: Definition[None], output: str) -> JsonValue:
    """RunResult.output as the judge sees it: JSON with an output model, else the text."""
    if definition.output is None:
        return output
    parsed: JsonValue = json.loads(output)
    return parsed


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
    events = await _events(env.store, lead.thread.branch)
    answer = _answer(target.definition, lead.output)
    text = case.meta.input.text if case.meta.input is not None else None
    judge = agent(
        name="judge", model=env.live.judge, instructions=JUDGE_V1, output=Verdicts, output_retries=1
    )
    thread = await _new_thread(env)
    ran = await _run(
        judge.definition, judge_input(text or "", events, answer, criteria), env, thread
    )
    agent_calls = await _requests(env.store, lead.thread.branch)
    if isinstance(ran, Blocked | Misconfigured):
        return _refused(ran, agent_calls, await _cost([lead.thread]))
    judge_calls = await _requests(env.store, ran.thread.branch)
    cost = await _cost([lead.thread, ran.thread])
    done = Outcome("error", agent_calls=agent_calls, judge_calls=judge_calls, cost=cost)
    if ran.status == "budget_exhausted":
        return Outcome(
            done.kind,
            reason="budget_exhausted",
            agent_calls=agent_calls,
            judge_calls=judge_calls,
            cost=cost,
        )
    graded = verdicts(json.loads(ran.output), criteria) if isinstance(ran, Completed) else None
    if graded is None:
        return Outcome(
            done.kind,
            reason="judge_invalid",
            agent_calls=agent_calls,
            judge_calls=judge_calls,
            cost=cost,
        )
    check = _check(graded, answer, events, env)
    if env.kept:
        check |= {"thread_id": lead.thread.id, "judge_thread_id": ran.thread.id}
    return Outcome("graded", check, agent_calls=agent_calls, judge_calls=judge_calls, cost=cost)


async def live_check(case: CaseDir, target: EvalAgent, env: Env) -> Outcome:
    """Runs the case input on the current agent and grades it."""
    criteria = [*(case.meta.rubric or ()), *env.live.rubric]
    if not criteria:
        return Outcome("skipped", reason="no_rubric")
    text = case.meta.input.text if case.meta.input is not None else None
    if text is None:
        return Outcome("skipped", reason="offline_not_runnable:content_input")
    thread = await _new_thread(env)
    lead = await _run(target.definition, text, env, thread, _recorded(case.stubs or {"stubs": []}))
    if isinstance(lead, Blocked | Misconfigured):
        return _refused(lead)
    if not isinstance(lead, Completed):
        calls = await _requests(env.store, lead.thread.branch)
        return Outcome(
            "error", reason=_unfinished(lead), agent_calls=calls, cost=await _cost([lead.thread])
        )
    return await _judged(case, target, lead, criteria, env)


def _unfinished(result: RunResult[str]) -> str:
    return f"failed: {result.error.code}" if isinstance(result, Failed) else result.status
