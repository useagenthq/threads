"""Hooks end to end through agent.run: each decision is a recorded hook_decision in the
wire vocabulary, gates fail closed, observers never change execution, injections render as
untrusted reference, and the hook set is part of the pinned config."""

import asyncio
import json
from collections.abc import Sequence

import pytest
from pydantic import BaseModel, JsonValue

from threads import Completed, ConfigError, Failed, RunContext, agent, scripted_model, sqlite, tool
from threads.agents.results import RunResult
from threads.agents.store import open_store
from threads.hooks.extension import extension
from threads.hooks.types import (
    Hooks,
    InputDecision,
    ModelGate,
    ResponseGate,
    ResultGate,
    Source,
    StopGate,
    ToolGate,
)
from threads.log import (
    Event,
    HookDecisionEvent,
    ModelResponseData,
    Permissions,
    Span,
    ToolCallData,
    ToolResultData,
    UserInputData,
)
from threads.log.jcs import canonicalize
from threads.loop.gates import MAX_RETRIES, MAX_STOP_CONTINUES
from threads.reduce.state import ReducedState
from threads.result import Ok

USAGE: JsonValue = {"input_tokens": 10, "output_tokens": 2}
LATE_S = 0.15
"""The 50 ms deadline plus slack, under the late hook's 200 ms cleanup: never awaited."""
ALLOW = Permissions(
    mode="default",
    allow=["echo"],
    ask=[],
    deny=[],
    protected_paths=[],
    allow_bypass=False,
    plan_exit_mode="default",
)


def text(reply: str) -> JsonValue:
    return {"content": [{"type": "text", "text": reply}], "stop_reason": "end_turn", "usage": USAGE}


def use(call_id: str = "call_1") -> JsonValue:
    part: JsonValue = {
        "type": "tool_use",
        "call_id": call_id,
        "name": "echo",
        "input": {"text": "hi"},
    }
    return {"content": [part], "stop_reason": "tool_use", "usage": USAGE}


class Echo(BaseModel):
    text: str


class Box:
    """The echo tool, counting how often its body ran."""

    def __init__(self) -> None:
        self.runs = 0

    async def run(self, args: Echo, _ctx: RunContext[None]) -> str:
        self.runs += 1
        return f"echo {args.text} SECRET=hunter2"


class Accent(Box):
    """An echo whose result starts with a two-byte character."""

    async def run(self, args: Echo, _ctx: RunContext[None]) -> str:
        return "\u00e9" + await super().run(args, _ctx)


async def run(
    hooks: Hooks,
    responses: Sequence[JsonValue],
    box: Box | None = None,
    permissions: Permissions = ALLOW,
) -> tuple[RunResult[str], list[Event]]:
    box = box or Box()
    echo = tool(name="echo", description="Echo.", input=Echo, runs="host", execute=box.run)
    bot = agent(
        model=scripted_model({"responses": list(responses)}),
        tools=[echo],
        permissions=permissions,
        extensions=[extension(name="ops", hooks=hooks, hook_timeout_ms=50)],
    )
    result = await bot.run("go", store=sqlite(":memory:"), deps=None)
    timeline = await result.thread.timeline()
    assert isinstance(timeline, Ok)
    return result, [e.event for e in timeline.value.entries]


def decisions(events: Sequence[Event]) -> list[tuple[str, str]]:
    return [(e.data.hook, e.data.decision) for e in events if isinstance(e, HookDecisionEvent)]


def kinds(events: Sequence[Event]) -> list[str]:
    return [e.type for e in events]


def test_before_tool_deny_is_folded_into_the_permission_decision_and_nothing_runs() -> None:
    async def deny(_call: ToolCallData, _ctx: RunContext[None]) -> ToolGate:
        return {"decision": "deny", "reason": "no echo today"}

    box = Box()
    result, events = asyncio.run(run({"before_tool": deny}, [use(), text("ok")], box))
    assert isinstance(result, Completed)
    assert box.runs == 0
    at = kinds(events).index("hook_decision")
    assert kinds(events)[at : at + 3] == ["hook_decision", "permission_decision", "tool_result"]
    permission = json.loads(events[at + 1].data.model_dump_json())
    assert (permission["source"], permission["reason"]) == ("hook", "no echo today")
    assert decisions(events) == [("before_tool", "deny")]


def test_before_tool_runs_even_when_the_policy_denies() -> None:
    async def allow(_call: ToolCallData, _ctx: RunContext[None]) -> ToolGate:
        return {"decision": "allow"}

    policy = ALLOW.model_copy(update={"allow": [], "deny": ["echo"]})
    _result, events = asyncio.run(run({"before_tool": allow}, [use(), text("ok")], None, policy))
    assert decisions(events) == [("before_tool", "allow")]
    permission = next(e for e in events if e.type == "permission_decision")
    assert json.loads(permission.data.model_dump_json())["decision"] == "deny"


def test_a_permission_request_deny_keeps_its_reason() -> None:
    async def refuse(_call: ToolCallData, _ctx: RunContext[None]) -> ToolGate:
        return {"decision": "deny", "reason": "on-call said no"}

    policy = ALLOW.model_copy(update={"allow": [], "ask": ["echo"]})
    _result, events = asyncio.run(
        run({"permission_request": refuse}, [use(), text("ok")], None, policy)
    )
    permission = next(e for e in events if e.type == "permission_decision")
    data = json.loads(permission.data.model_dump_json())
    assert (data["decision"], data["source"], data["reason"]) == ("deny", "hook", "on-call said no")


def test_a_permission_request_ask_leaves_the_policy_decision() -> None:
    async def unsure(_call: ToolCallData, _ctx: RunContext[None]) -> ToolGate:
        return {"decision": "ask", "rule": "echo(*)"}

    policy = ALLOW.model_copy(update={"allow": [], "ask": ["echo"]})
    _result, events = asyncio.run(
        run({"permission_request": unsure}, [use(), text("ok")], None, policy)
    )
    permission = next(e for e in events if e.type == "permission_decision")
    data = json.loads(permission.data.model_dump_json())
    assert (data["decision"], data["source"], "reason" in data) == ("ask", "policy", False)


def test_a_before_tool_ask_records_its_rule_as_the_reason() -> None:
    async def ask(_call: ToolCallData, _ctx: RunContext[None]) -> ToolGate:
        return {"decision": "ask", "rule": "echo(*)"}

    _result, events = asyncio.run(run({"before_tool": ask}, [use(), text("ok")]))
    permission = next(e for e in events if e.type == "permission_decision")
    data = json.loads(permission.data.model_dump_json())
    assert (data["decision"], data["reason"]) == ("ask", "echo(*)")


@pytest.mark.parametrize("how", ["raise", "timeout", "garbage"])
def test_a_failing_gate_fails_closed(how: str) -> None:
    async def broken(_call: ToolCallData, _ctx: RunContext[None]) -> JsonValue:
        if how == "raise":
            raise RuntimeError("hook crashed")
        if how == "timeout":
            await asyncio.sleep(1)
        return {"decision": "maybe"}

    box = Box()
    result, events = asyncio.run(run({"before_tool": broken}, [use(), text("ok")], box))  # type: ignore[typeddict-item]  # a deliberately broken hook
    assert isinstance(result, Completed)
    assert box.runs == 0
    assert decisions(events) == [("before_tool", "failed")]
    permission = next(e for e in events if e.type == "permission_decision")
    assert json.loads(permission.data.model_dump_json())["decision"] == "deny"


def test_on_stop_continuations_are_bounded_then_stop_hook_limit() -> None:
    async def again(_state: ReducedState, _ctx: RunContext[None]) -> StopGate:
        return {"decision": "continue", "reason": "keep going"}

    replies = [text(f"r{n}") for n in range(5)]
    result, events = asyncio.run(run({"on_stop": again}, replies))
    assert isinstance(result, Failed)
    assert result.error.code == "stop_hook_limit"
    rounds = MAX_STOP_CONTINUES + 1
    assert decisions(events) == [("on_stop", "continue")] * rounds
    assert kinds(events).count("model_request") == rounds


def test_a_failing_after_tool_observer_is_recorded_and_the_tool_never_reruns() -> None:
    async def boom(_c: ToolCallData, _r: ToolResultData, _ctx: RunContext[None]) -> Sequence[str]:
        raise RuntimeError("audit sink down")

    box = Box()
    result, events = asyncio.run(run({"after_tool": boom}, [use(), text("ok")], box))
    assert isinstance(result, Completed)
    assert box.runs == 1
    assert decisions(events) == [("after_tool", "failed")]


def test_before_input_deny_keeps_the_input_and_ends_the_turn() -> None:
    async def guard(_input: UserInputData, _ctx: RunContext[None]) -> InputDecision:
        return {"decision": "deny", "reason": "off topic"}

    result, events = asyncio.run(run({"before_input": guard}, [text("never")]))
    assert isinstance(result, Failed)
    assert result.error.code == "input_denied"
    assert "user_input" in kinds(events)
    assert "model_request" not in kinds(events)


def test_before_model_injections_render_as_untrusted_reference() -> None:
    async def inject(_state: ReducedState, _ctx: RunContext[None]) -> ModelGate:
        return {"decision": "proceed", "injections": ["on-call: alice"]}

    result, events = asyncio.run(run({"before_model": inject}, [text("ok")]))
    assert isinstance(result, Completed)
    injected = next(e for e in events if e.type == "injected")
    data = json.loads(injected.data.model_dump_json())
    assert (data["source"], data["trust"]) == ("hook", "untrusted_reference")
    assert kinds(events).index("injected") < kinds(events).index("model_request")


def test_before_tool_result_redaction_hides_the_span_from_later_requests() -> None:
    async def redact(
        _c: ToolCallData, result: ToolResultData, _ctx: RunContext[None]
    ) -> ResultGate:
        start = result.preview.encode().index(b"hunter2")
        return {"decision": "redact", "spans": [Span(start=start, end=start + 7)]}

    async def main() -> None:
        _result, events = await run({"before_tool_result": redact}, [use(), text("ok")])
        edited = next(e for e in events if e.type == "context_edited")
        assert json.loads(edited.data.model_dump_json())["reason"] == "guardrail"
        assert decisions(events) == [("before_tool_result", "redact")]

    asyncio.run(main())


@pytest.mark.parametrize(
    "spans", [[], [Span(start=0, end=10_000)], [Span(start=1, end=2)]], ids=["none", "out", "split"]
)
def test_a_bad_redaction_clears_the_result_instead_of_crashing(spans: list[Span]) -> None:
    async def redact(_c: ToolCallData, _r: ToolResultData, _ctx: RunContext[None]) -> ResultGate:
        return {"decision": "redact", "spans": spans}

    async def main() -> None:
        # The echo result starts with a two-byte character: offset 1 splits it.
        result, events = await run({"before_tool_result": redact}, [use(), text("ok")], Accent())
        assert isinstance(result, Completed)
        assert decisions(events) == [("before_tool_result", "failed")]
        edited = next(e for e in events if e.type == "context_edited")
        edits = json.loads(edited.data.model_dump_json())["edits"]
        assert edits == [{"call_id": "call_1", "action": "clear"}]

    asyncio.run(main())


def test_after_model_deny_closes_the_calls_and_withholds_the_output() -> None:
    async def veto(_s: ReducedState, _r: ModelResponseData, _ctx: RunContext[None]) -> ResponseGate:
        return {"decision": "deny", "reason": "unsafe"}

    box = Box()
    result, events = asyncio.run(run({"after_model": veto}, [use()], box))
    assert isinstance(result, Failed)
    assert box.runs == 0
    closed = next(e for e in events if e.type == "tool_result")
    assert json.loads(closed.data.model_dump_json())["origin"] == "denied"


def test_session_start_and_after_tool_batch_inject_before_their_step() -> None:
    async def start(_source: Source, _ctx: RunContext[None]) -> Sequence[str]:
        return ["runbook v2"]

    async def batch(_state: ReducedState, _ctx: RunContext[None]) -> Sequence[str]:
        return ["remember to cite"]

    result, events = asyncio.run(
        run({"session_start": start, "after_tool_batch": batch}, [use(), text("ok")])
    )
    assert isinstance(result, Completed)
    assert decisions(events) == [("session_start", "proceed"), ("after_tool_batch", "proceed")]
    order = kinds(events)
    assert order.index("injected") < order.index("user_input")
    assert order.count("injected") == len(["runbook v2", "remember to cite"])


def test_the_hook_set_is_pinned_and_a_changed_one_starts_no_run() -> None:
    async def stop(_state: ReducedState, _ctx: RunContext[None]) -> StopGate:
        return {"decision": "stop"}

    async def main() -> None:
        store = sqlite(":memory:")
        pinned = extension(name="ops", hooks={"on_stop": stop})
        first = await agent(
            model=scripted_model({"responses": [text("a")]}), extensions=[pinned]
        ).run("go", store=store, deps=None)
        started = await first.thread.timeline()
        assert isinstance(started, Ok)
        config_hash = json.loads(started.value.entries[0].event.data.model_dump_json())[
            "config_hash"
        ]
        sq = await open_store(store)
        config = await sq.get_artifact(config_hash)
        assert isinstance(config, Ok)
        manifest = json.loads(config.value)["extensions"]
        assert manifest == [
            {"name": "ops", "hooks": ["on_stop"], "observers": [], "hook_timeout_ms": 5000}
        ]
        canonical = canonicalize(json.loads(config.value))
        assert isinstance(canonical, Ok)
        assert canonical.value.encode() == config.value
        changed = extension(name="ops")
        with pytest.raises(ConfigError):
            await agent(model=scripted_model({"responses": [text("b")]}), extensions=[changed]).run(
                "more", store=store, thread=first.thread, deps=None
            )

    asyncio.run(main())


def test_a_gate_that_swallows_its_cancellation_still_fails_at_the_deadline() -> None:
    """a hook that catches the timeout's cancellation and answers late must not
    authorize anything; the deadline alone decides."""

    async def late(_state: ReducedState, _ctx: RunContext[None]) -> ModelGate:
        try:
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            await asyncio.sleep(0.2)
        return {"decision": "proceed"}

    async def main() -> None:
        started = asyncio.get_running_loop().time()
        result, events = await run({"before_model": late}, [text("bypassed")])
        assert isinstance(result, Failed)
        assert decisions(events) == [("before_model", "failed")]
        assert "model_request" not in kinds(events)
        assert asyncio.get_running_loop().time() - started < LATE_S

    asyncio.run(main())


def test_after_model_retries_are_capped_then_denied() -> None:
    answers: list[ResponseGate] = [
        {"decision": "retry", "reason": "again"},
        {"decision": "retry", "reason": "again"},
        {"decision": "retry", "reason": "again"},
        {"decision": "proceed"},
    ]

    async def retry(
        _s: ReducedState, _r: ModelResponseData, _ctx: RunContext[None]
    ) -> ResponseGate:
        return answers.pop(0)

    replies = [text(f"r{n}") for n in range(5)]
    result, events = asyncio.run(run({"after_model": retry}, replies))
    assert isinstance(result, Failed)
    assert kinds(events).count("model_request") == MAX_RETRIES + 1
    assert decisions(events)[-1] == ("after_model", "retry")
