"""The hooks around a tool call: what may run, who approves it, what the model is shown, and
what one response's whole batch of calls leads to."""

from collections.abc import Mapping, Sequence

from each_kit import (
    Box,
    Decision,
    HookCase,
    kinds,
    last_at,
    logged,
    normalize,
    only_decisions_differ,
    ops,
    part,
    say,
    use,
    uses,
)
from pydantic import JsonValue

from threads import RunContext, agent, scripted_model, sqlite
from threads.hooks.extension import Extension
from threads.hooks.types import ResultGate, ToolGate
from threads.log import Event, Permissions, Span, ToolCallData, ToolResultData, ToolResultEvent
from threads.reduce.state import ReducedState


def rules(
    *, allow: Sequence[str] = (), ask: Sequence[str] = (), deny: Sequence[str] = ()
) -> Permissions:
    """The permissions a case runs under; only the three rule lists ever differ."""
    return Permissions(
        mode="default",
        allow=list(allow),
        ask=list(ask),
        deny=list(deny),
        protected_paths=[],
        allow_bypass=False,
        plan_exit_mode="default",
    )


ALLOW = rules(allow=["echo"])
BATCH = 2
"""Calls in the one response the after_tool_batch case sends."""
TURN: Sequence[JsonValue] = (use(), say("ok"))


async def ran(
    responses: Sequence[JsonValue],
    exts: Sequence[Extension[None]],
    permissions: Permissions = ALLOW,
) -> tuple[str, int, list[Event]]:
    """One run of the echo turn under `rules`, with `exts` as its extensions."""
    box = Box()
    bot = agent(
        model=scripted_model({"responses": list(responses)}),
        tools=[box.tool],
        permissions=permissions,
        extensions=list(exts),
    )
    result = await bot.run("go", store=sqlite(":memory:"), deps=None)
    return result.status, box.runs, await logged(result.thread)


async def _before_tool() -> Sequence[Decision]:
    seen: list[str] = []

    async def deny(call: ToolCallData, _ctx: RunContext[None]) -> ToolGate:
        seen.append(f"{call.name}:{call.call_id}")
        return {"decision": "deny", "reason": "no echo today"}

    status, runs, log = await ran(TURN, [ops({"before_tool": deny})])
    assert status == "completed"
    assert seen == ["echo:call_1"]
    # The deny is folded into the call's one permission_decision; nothing runs.
    assert runs == 0
    at = kinds(log).index("hook_decision")
    assert kinds(log)[at : at + 3] == ["hook_decision", "permission_decision", "tool_result"]
    denied = log[at + 2]
    assert isinstance(denied, ToolResultEvent)
    assert denied.data.preview == "denied: no echo today"
    return normalize(log)


async def _permission_request() -> Sequence[Decision]:
    seen: list[str] = []

    async def approve(call: ToolCallData, _ctx: RunContext[None]) -> ToolGate:
        seen.append(call.call_id)
        return {"decision": "allow"}

    status, runs, log = await ran(TURN, [ops({"permission_request": approve})], rules(ask=["echo"]))
    assert status == "completed"
    assert seen == ["call_1"]
    # The hook answered the policy's ask, so the call ran without an approval challenge.
    assert runs == 1
    assert "approval_requested" not in kinds(log)
    decided = next(e for e in log if e.type == "permission_decision")
    assert (decided.data.decision, decided.data.source) == ("allow", "hook")
    return normalize(log)


async def _permission_denied() -> Sequence[Decision]:
    seen: list[str] = []

    async def boom(call: ToolCallData, _ctx: RunContext[None]) -> None:
        seen.append(call.name)
        raise RuntimeError("pager down")

    denied = rules(deny=["echo"])
    status, runs, log = await ran(TURN, [ops({"permission_denied": boom})], denied)
    _plain_status, _plain_runs, plain = await ran(TURN, [], denied)
    assert status == "completed"
    assert seen == ["echo"]
    assert runs == 0
    only_decisions_differ(log, plain)
    return normalize(log)


async def _after_tool() -> Sequence[Decision]:
    seen: list[str] = []

    async def boom(
        call: ToolCallData, result: ToolResultData, _ctx: RunContext[None]
    ) -> Sequence[str]:
        seen.append(f"{call.name}:{result.origin}")
        raise RuntimeError("audit sink down")

    status, runs, log = await ran(TURN, [ops({"after_tool": boom})])
    _plain_status, _plain_runs, plain = await ran(TURN, [])
    assert status == "completed"
    assert seen == ["echo:executed"]
    # The effect already happened: an observer's failure never re-runs it.
    assert runs == 1
    only_decisions_differ(log, plain)
    return normalize(log)


async def _before_tool_result() -> Sequence[Decision]:
    seen: list[str] = []

    async def redact(
        call: ToolCallData, result: ToolResultData, _ctx: RunContext[None]
    ) -> ResultGate:
        seen.append(call.call_id)
        start = (result.preview or "").encode().index(b"hunter2")
        return {"decision": "redact", "spans": [Span(start=start, end=start + len(b"hunter2"))]}

    status, _runs, log = await ran(TURN, [ops({"before_tool_result": redact})])
    assert status == "completed"
    assert seen == ["call_1"]
    edited = next(e for e in log if e.type == "context_edited")
    assert edited.data.reason == "guardrail"
    return normalize(log)


async def _after_tool_batch() -> Sequence[Decision]:
    seen: list[int] = []

    async def batch(state: ReducedState, _ctx: RunContext[None]) -> Sequence[str]:
        seen.append(state.turns_completed)
        return ["remember to cite"]

    status, runs, log = await ran(
        [uses(part("call_1"), part("call_2")), say("ok")],
        [ops({"after_tool_batch": batch})],
    )
    assert status == "completed"
    # Once, after every result of the one response is in.
    assert len(seen) == 1
    assert runs == BATCH
    order = kinds(log)
    assert last_at(order, "tool_result") < order.index("injected")
    assert order.index("injected") < last_at(order, "model_request")
    return normalize(log)


TOOL_CASES: Mapping[str, HookCase] = {
    "before_tool": _before_tool,
    "permission_request": _permission_request,
    "permission_denied": _permission_denied,
    "after_tool": _after_tool,
    "before_tool_result": _before_tool_result,
    "after_tool_batch": _after_tool_batch,
}
