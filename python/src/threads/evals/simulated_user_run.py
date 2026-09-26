"""Who plays the user of a simulated case (spec lane 32, B.2.4 and C): fixed messages, or a threads
agent on its own thread, so every user turn is on the log. Both answer the same question -- what
does the user say next -- and both end the conversation as a value."""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

from threads._generated.eval_v1 import (
    CaseSimulate,
    CaseSimulate1,
    CaseSimulate2,
    UserTurn,
)
from threads.agents.factory import agent
from threads.agents.results import Completed, Failed, RunResult, Thread
from threads.evals.live_env import Blocked, Env, Misconfigured, run_agent, unfinished
from threads.evals.simulated_user import (
    VisibleMessage,
    simulated_user_input,
    user_instructions,
    user_turn,
)
from threads.evals.tree_totals import tree_totals
from threads.log import Budget
from threads.loop.guard import blocked_model
from threads.loop.model import Model

if TYPE_CHECKING:
    from pydantic import JsonValue


@dataclass(frozen=True, slots=True)
class Play:
    kind: Literal["message", "ended", "error", "blocked"]
    text: str = ""
    """kind message: what the user says next. kind blocked: the model that was blocked."""
    ending: Literal["user_done", "script_done", "max_messages"] = "user_done"
    reason: str = ""


class UserPlayer(Protocol):
    """What the turn loop needs of whoever plays the user."""

    async def next(self, seen: Sequence[VisibleMessage], budget: Budget) -> Play: ...

    @property
    def thread(self) -> Thread | None: ...

    async def calls(self) -> int: ...


class ScriptPlayer:
    """Fixed messages, sent in order after the opener: no user model is needed."""

    def __init__(self, messages: Sequence[str]) -> None:
        self._messages = list(messages)
        self._at = 0

    async def next(self, seen: Sequence[VisibleMessage], budget: Budget) -> Play:
        del seen, budget
        if self._at >= len(self._messages):
            return Play("ended", ending="script_done")
        text = self._messages[self._at]
        self._at += 1
        return Play("message", text)

    @property
    def thread(self) -> Thread | None:
        return None

    async def calls(self) -> int:
        return 0


def _played(ran: RunResult[str]) -> Play:
    """What the simulator's run says: the next message, the user stopping, or why it failed."""
    if isinstance(ran, Failed):
        return Play("error", reason="simulator_invalid")
    if not isinstance(ran, Completed):
        return Play("error", reason=unfinished(ran))
    parsed: JsonValue = json.loads(ran.output)
    turn = user_turn(parsed)
    if turn is None:
        return Play("error", reason="simulator_invalid")
    if turn.done:
        return Play("ended", ending="user_done")
    return Play("message", turn.message)


class ModelPlayer:
    """A model playing the persona and goal, on its own thread (tenant evals)."""

    def __init__(self, simulate: CaseSimulate1, model: Model, env: Env) -> None:
        self._user = agent(
            name="simulated_user",
            model=model,
            instructions=user_instructions(simulate.persona, simulate.goal),
            output=UserTurn,
            output_retries=1,
        )
        self._env = env
        self._thread: Thread | None = None

    async def next(self, seen: Sequence[VisibleMessage], budget: Budget) -> Play:
        ran = await run_agent(
            self._user.definition, simulated_user_input(seen), self._env, self._thread, budget
        )
        if isinstance(ran, Blocked):
            return Play("blocked", ran.model)
        if isinstance(ran, Misconfigured):
            return Play("error", reason=ran.reason)
        self._thread = ran.thread
        return _played(ran)

    @property
    def thread(self) -> Thread | None:
        return self._thread

    async def calls(self) -> int:
        if self._thread is None:
            return 0
        return (await tree_totals(self._thread.store, self._thread.branch)).requests


def player_for(simulate: CaseSimulate, env: Env) -> UserPlayer | Blocked:
    """The player for a case, or the guard's block: a model user is checked before the first agent
    run, so a blocked user model dispatches nothing at all."""
    if isinstance(simulate, CaseSimulate2):
        return ScriptPlayer(simulate.messages)
    model = env.live.user
    if model is None:
        raise AssertionError("a model-kind case is checked for live.user before it runs")
    named = blocked_model(model)
    if named is not None:
        return Blocked(named)
    return ModelPlayer(simulate, model, env)
