"""The hooks around a turn: the session, the input, each request and response, and how a turn
ends. One public agent().run() per hook, in the situation lane 14 E names."""

from collections.abc import Mapping, Sequence

from each_kit import (
    OVERLOADED,
    Decision,
    Echo,
    HookCase,
    kinds,
    logged,
    normalize,
    only_decisions_differ,
    ops,
    part,
    quick,
    say,
    use,
    uses,
)
from pydantic import JsonValue

from threads import RunContext, agent, scripted_model, sqlite, tool
from threads.hooks.extension import Extension
from threads.hooks.types import (
    InputDecision,
    ModelGate,
    ResponseGate,
    Source,
    StopGate,
)
from threads.log import Event, ModelResponseData, Principal, Retry, UserInputData
from threads.reduce.state import ReducedState
from threads.result import Ok
from threads.thread.handle import open_thread

QUICK = quick()
OPERATOR = Principal(issuer="api", tenant="local", subject="operator")
ROUNDS = 2
"""on_stop rounds in the on_stop case: one continue, then the stop that ends the turn."""


async def ran(
    responses: Sequence[JsonValue],
    exts: Sequence[Extension[None]],
    retry: Retry = QUICK,
) -> tuple[str, list[Event]]:
    """One run with `exts` as its extensions, and its branch's log."""
    bot = agent(
        model=scripted_model({"responses": list(responses)}),
        extensions=list(exts),
        retry=retry,
    )
    result = await bot.run("go", store=sqlite(":memory:"), deps=None)
    return result.status, await logged(result.thread)


async def cancelled_run(ext: Extension[None]) -> str:
    """A run cancelled from inside a tool, so the loop ends the turn cancelled."""
    store = sqlite(":memory:")

    async def stop(_args: Echo, ctx: RunContext[None]) -> str:
        opened = await open_thread(store, ctx.thread_id)
        assert isinstance(opened, Ok)
        assert isinstance(await opened.value.cancel(OPERATOR), Ok)
        return "stopping"

    bot = agent(
        model=scripted_model(
            {"responses": [uses(part("call_1", "stop_now", {"text": "x"})), say("never")]}
        ),
        tools=[
            tool(
                name="stop_now",
                description="Cancel this run.",
                input=Echo,
                runs="host",
                effect="read_only",
                execute=stop,
            )
        ],
        permissions={"allow": ["stop_now"], "ask": [], "deny": []},
        extensions=[ext],
    )
    result = await bot.run("go", store=store, deps=None, principal=OPERATOR)
    return result.status


async def _session_start() -> Sequence[Decision]:
    seen: list[str] = []

    async def start(source: Source, ctx: RunContext[None]) -> Sequence[str]:
        seen.append(f"{source}:{ctx.branch_id != ''}")
        return ["runbook v2"]

    status, log = await ran([say("ok")], [ops({"session_start": start})])
    assert status == "completed"
    assert seen == ["startup:True"]
    # The injection is recorded with the decision, before the input it feeds.
    assert kinds(log)[1:4] == ["hook_decision", "injected", "user_input"]
    return normalize(log)


async def _session_end() -> Sequence[Decision]:
    seen: list[str] = []

    async def end(ctx: RunContext[None]) -> None:
        seen.append(ctx.thread_id)
        raise RuntimeError("audit sink down")

    status, log = await ran([say("ok")], [ops({"session_end": end})])
    _plain_status, plain = await ran([say("ok")], [])
    assert status == "completed"
    assert len(seen) == 1
    only_decisions_differ(log, plain)
    # A run that ends cancelled is still a run that ended: the hook runs there too.
    ends: list[str] = []

    async def note(ctx: RunContext[None]) -> None:
        ends.append(ctx.thread_id)

    assert await cancelled_run(ops({"session_end": note})) == "cancelled"
    assert len(ends) == 1
    return normalize(log)


async def _before_input() -> Sequence[Decision]:
    seen: list[str] = []

    async def guard(data: UserInputData, _ctx: RunContext[None]) -> InputDecision:
        seen.append(str(data.text or ""))
        return {"decision": "deny", "reason": "off topic"}

    status, log = await ran([say("never")], [ops({"before_input": guard})])
    assert status == "failed"
    assert seen == ["go"]
    # The input stays in the log for audit; no request is ever sent.
    assert "user_input" in kinds(log)
    assert "model_request" not in kinds(log)
    return normalize(log)


async def _before_model() -> Sequence[Decision]:
    seen: list[int] = []

    async def gate(state: ReducedState, _ctx: RunContext[None]) -> ModelGate:
        seen.append(state.turns_completed)
        return {"decision": "deny", "reason": "frozen window"}

    status, log = await ran([say("never")], [ops({"before_model": gate})])
    assert status == "failed"
    assert seen == [0]
    assert "model_request" not in kinds(log)
    return normalize(log)


async def _after_model() -> Sequence[Decision]:
    seen: list[str] = []

    async def veto(
        _state: ReducedState, response: ModelResponseData, _ctx: RunContext[None]
    ) -> ResponseGate:
        seen.append(response.stop_reason)
        return {"decision": "deny", "reason": "unsafe"}

    status, log = await ran([use()], [ops({"after_model": veto})])
    assert status == "failed"
    assert seen == ["tool_use"]
    # The response's call is closed denied without running, and the model sees why.
    closed = next(e for e in log if e.type == "tool_result")
    assert (closed.data.origin, closed.data.preview) == ("denied", "denied: unsafe")
    return normalize(log)


async def _on_stop() -> Sequence[Decision]:
    answers: list[StopGate] = [
        {"decision": "continue", "reason": "keep going"},
        {"decision": "stop"},
    ]
    rounds: list[int] = []

    async def again(_state: ReducedState, _ctx: RunContext[None]) -> StopGate:
        rounds.append(1)
        return answers.pop(0)

    status, log = await ran([say("first"), say("second")], [ops({"on_stop": again})])
    assert status == "completed"
    assert len(rounds) == ROUNDS
    # The continue is a trusted instruction, so the turn asks again.
    assert kinds(log).count("model_request") == ROUNDS
    return normalize(log)


async def _on_stop_failure() -> Sequence[Decision]:
    seen: list[str] = []
    stops: list[int] = []

    async def stop(_state: ReducedState, _ctx: RunContext[None]) -> StopGate:
        stops.append(1)
        return {"decision": "stop"}

    async def failure(code: str, _ctx: RunContext[None]) -> None:
        seen.append(code)
        raise RuntimeError("pager down")

    retry = quick(max_retries=0)
    status, log = await ran(
        [OVERLOADED], [ops({"on_stop": stop, "on_stop_failure": failure})], retry
    )
    _plain_status, plain = await ran([OVERLOADED], [], retry)
    assert status == "failed"
    assert seen == ["model_unavailable"]
    # on_stop never runs for a turn that ends in a failure.
    assert stops == []
    only_decisions_differ(log, plain)
    return normalize(log)


async def _notification() -> Sequence[Decision]:
    seen: list[str] = []

    async def notify(event: Event, _ctx: RunContext[None]) -> None:
        seen.append(event.type)
        raise RuntimeError("webhook timed out")

    replies = [OVERLOADED, say("ok")]
    status, log = await ran(replies, [ops({"notification": notify})], QUICK)
    _plain_status, plain = await ran(replies, [], QUICK)
    assert status == "completed"
    # The retry wait is what a notification observer is told about.
    assert seen == ["retry_scheduled"]
    only_decisions_differ(log, plain)
    return normalize(log)


TURN_CASES: Mapping[str, HookCase] = {
    "session_start": _session_start,
    "session_end": _session_end,
    "before_input": _before_input,
    "before_model": _before_model,
    "after_model": _after_model,
    "on_stop": _on_stop,
    "on_stop_failure": _on_stop_failure,
    "notification": _notification,
}
