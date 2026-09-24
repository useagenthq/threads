"""Hooks end to end through agent.run: each decision is a recorded hook_decision in the
wire vocabulary, gates fail closed, observers never change execution, injections render as
untrusted reference, and the hook set is part of the pinned config."""

import asyncio
import json
from collections.abc import Sequence

import pytest
from hook_kit import Box, decisions, kinds, run, text, use
from pydantic import JsonValue

from threads import Completed, ConfigError, Failed, RunContext, agent, scripted_model, sqlite
from threads.agents.store import open_store
from threads.hooks.extension import extension
from threads.hooks.types import (
    InputDecision,
    ModelGate,
    ResponseGate,
    Source,
    StopGate,
    ToolGate,
)
from threads.log import (
    HookDecisionEvent,
    ModelResponseData,
    ToolCallData,
    ToolResultData,
    UserInputData,
)
from threads.log.jcs import canonicalize
from threads.loop.gates import MAX_RETRIES, MAX_STOP_CONTINUES
from threads.reduce.state import ReducedState
from threads.result import Ok

LATE_S = 0.15
"""The 50 ms deadline plus slack, under the late hook's 200 ms cleanup: never awaited."""


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


def test_after_model_deny_closes_the_calls_and_withholds_the_output() -> None:
    async def veto(_s: ReducedState, _r: ModelResponseData, _ctx: RunContext[None]) -> ResponseGate:
        return {"decision": "deny", "reason": "unsafe"}

    box = Box()
    result, events = asyncio.run(run({"after_model": veto}, [use()], box))
    assert isinstance(result, Failed)
    assert box.runs == 0
    closed = next(e for e in events if e.type == "tool_result")
    data = json.loads(closed.data.model_dump_json())
    # The model is shown the hook's reason, as in TypeScript.
    assert (data["origin"], data["preview"]) == ("denied", "denied: unsafe")


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
    # Past the cap the answer is recorded as a deny, as TypeScript records it.
    assert decisions(events)[-1] == ("after_model", "deny")
    last = [e for e in events if isinstance(e, HookDecisionEvent)][-1]
    assert last.data.reason == "retry limit reached: again"


def test_each_call_is_recorded_with_its_before_tool_decision_before_the_next() -> None:
    """spec/schema/README.md, "Recording a response's calls": the same order TypeScript writes."""

    async def gate(call: ToolCallData, _ctx: RunContext[None]) -> ToolGate:
        if call.call_id == "call_2":
            return {"decision": "deny", "reason": "nope"}
        return {"decision": "allow"}

    both: JsonValue = {
        **_response(use("call_1")),
        "content": [*_parts(use("call_1")), *_parts(use("call_2"))],
    }
    box = Box()
    result, events = asyncio.run(run({"before_tool": gate}, [both, text("ok")], box))
    assert isinstance(result, Completed)
    assert box.runs == 1
    start = kinds(events).index("model_response") + 1
    assert kinds(events)[start : start + 7] == [
        "tool_call",
        "hook_decision",
        "permission_decision",
        "tool_call",
        "hook_decision",
        "permission_decision",
        "effect_begin",
    ]
    denied = [e for e in events if e.type == "tool_result"][1]
    assert json.loads(denied.data.model_dump_json())["preview"] == "denied: nope"


def _response(response: JsonValue) -> dict[str, JsonValue]:
    assert isinstance(response, dict)
    return response


def _parts(response: JsonValue) -> list[JsonValue]:
    parts = _response(response)["content"]
    assert isinstance(parts, list)
    return parts
