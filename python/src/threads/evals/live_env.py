"""What the live checks share (spec lane 22, C and lane 32, B): the options a live run is given,
the eval principal, one run of an agent as a value, and reading a live thread's log back."""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal, Protocol

from pydantic import JsonValue

from threads.agents.config import ConfigError
from threads.agents.definition import Definition
from threads.agents.results import Failed, RunResult, StreamEvent, Thread
from threads.agents.run import RunOptions, execute
from threads.agents.store import Store, now_ms, open_store
from threads.evals.report import add_cost
from threads.log import BranchId, Budget, Cost, Event, Principal, ThreadId, UnknownEvent
from threads.loop.guard import ModelBlockedError
from threads.loop.model import Model
from threads.loop.stubs import Stubs
from threads.result import Ok
from threads.store.lines import uuid7
from threads.thread.read import read_log

EVAL_PRINCIPAL: Final = Principal(issuer="threads", tenant="evals", subject="eval-runner")


@dataclass(frozen=True, slots=True)
class Live:
    """What `live` / `--live` gives: the judge model, the budget of every run (the existing
    `Budget`), criteria added after each case's own, and the model that plays a simulated user."""

    judge: Model
    budget: Budget
    rubric: tuple[str, ...] = ()
    user: Model | None = None


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
class Simulation:
    """EvalCaseResult.simulation (spec lane 32, E): what the conversation did."""

    messages: int
    prefix: Literal["continued", "redriven", "none"]
    prefix_turns: int
    ended: Literal["user_done", "script_done", "max_messages"]
    user_thread_id: str | None = None

    def to_json(self) -> JsonValue:
        out: dict[str, JsonValue] = {
            "messages": self.messages,
            "prefix": self.prefix,
            "prefix_turns": self.prefix_turns,
            "ended": self.ended,
        }
        if self.user_thread_id is not None:
            out["user_thread_id"] = self.user_thread_id
        return out


@dataclass(frozen=True, slots=True)
class Outcome:
    kind: Literal["graded", "error", "skipped", "blocked"]
    check: JsonValue = None
    reason: str = ""
    agent_calls: int = 0
    user_calls: int = 0
    judge_calls: int = 0
    cost: Cost | None = None
    simulation: Simulation | None = None


@dataclass(frozen=True, slots=True)
class Blocked:
    """The model-request guard stopped the run before dispatch."""

    model: str


@dataclass(frozen=True, slots=True)
class Misconfigured:
    """The agent's setup failed: a ConfigError, as its code and message."""

    reason: str


def _drop(_item: StreamEvent) -> None:
    pass


async def events_of(store: Store, branch: BranchId) -> tuple[Event, ...]:
    """A thread's events, read from the store: the log is the truth the report derives from."""
    read = await read_log(store, branch)
    if not isinstance(read, Ok):
        return ()
    return tuple(e for e in read.value.fold.events if not isinstance(e, UnknownEvent))


async def cost_sum(threads: Sequence[Thread]) -> Cost | None:
    """Thread.cost(tree=True) summed; None once any part ran unpriced."""
    total: Cost | None = None
    for i, t in enumerate(threads):
        got = await t.cost(tree=True)
        total = add_cost(total, got.value if isinstance(got, Ok) else None, first=i == 0)
    return total


async def new_thread(env: Env) -> Thread:
    """A new thread for one live run or one simulated conversation."""
    sq = await open_store(env.store)
    now = now_ms()
    thread_id, branch_id = ThreadId(uuid7(now)), BranchId(uuid7(now))
    await sq.create(thread_id, branch_id, now)
    return Thread(thread_id, branch_id, env.store)


async def run_agent(  # noqa: PLR0913, PLR0917 - one run, and what it answers from
    definition: Definition[None],
    text: str,
    env: Env,
    thread: Thread | None,
    budget: Budget | None = None,
    stubs: Stubs | None = None,
) -> RunResult[str] | Blocked | Misconfigured:
    """One run: its result, a guard block, or a setup mistake. Any other raise is a bug. A
    simulated conversation passes one gateway every turn, so its stub queue spans them all."""
    on = thread if thread is not None else await new_thread(env)
    options: RunOptions[None] = {
        "store": env.store,
        "budget": budget if budget is not None else env.live.budget,
        "principal": EVAL_PRINCIPAL,
        "thread": on,
    }
    try:
        return await execute(definition, text, options, None, _drop, stubs=stubs)
    except ModelBlockedError as blocked:
        return Blocked(blocked.model)
    except ConfigError as error:
        return Misconfigured(f"{error.code}: {error.message}")


def answer_of(definition: Definition[None], output: str) -> JsonValue:
    """RunResult.output as the judge sees it: JSON with an output model, else the text."""
    if definition.output is None:
        return output
    parsed: JsonValue = json.loads(output)
    return parsed


def unfinished(result: RunResult[str]) -> str:
    """A run that didn't complete, as the case's error reason."""
    return f"failed: {result.error.code}" if isinstance(result, Failed) else result.status
