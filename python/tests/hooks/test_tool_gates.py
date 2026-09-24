"""Tool-call hooks end to end: before_tool and permission_request fold into the call's one
permission_decision, and before_tool_result redacts or clears what later requests render. The
same behavior as TypeScript's test/loop/hooks.test.ts."""

import asyncio
import json

import pytest
from hook_kit import ALLOW, Accent, Box, Echo, decisions, kinds, run, text, use

from threads import Completed, RunContext, agent, scripted_model, sqlite, tool
from threads.hooks.extension import extension
from threads.hooks.types import Hooks, ResultGate, ToolGate
from threads.log import HookDecisionEvent, Span, ToolCallData, ToolResultData
from threads.result import Ok


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
    # The model is shown the deny's reason, as in TypeScript.
    assert json.loads(events[at + 2].data.model_dump_json())["preview"] == "denied: no echo today"
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
    "spans",
    [[], [Span(start=0, end=10_000)], [Span(start=1, end=2)], [Span(start=0, end=0)]],
    ids=["none", "out", "split", "zero-width"],
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


def test_an_ask_rule_is_recorded_then_permission_request_answers_the_ask() -> None:
    async def ask(_call: ToolCallData, _ctx: RunContext[None]) -> ToolGate:
        return {"decision": "ask", "rule": "echo(*)"}

    async def approve(_call: ToolCallData, _ctx: RunContext[None]) -> ToolGate:
        return {"decision": "allow"}

    box = Box()
    hooks: Hooks[None] = {"before_tool": ask, "permission_request": approve}
    result, events = asyncio.run(run(hooks, [use(), text("ok")], box))
    assert isinstance(result, Completed)
    assert box.runs == 1
    recorded = [
        json.loads(e.data.model_dump_json()) for e in events if isinstance(e, HookDecisionEvent)
    ]
    assert [(d["hook"], d["decision"], d.get("reason")) for d in recorded] == [
        ("before_tool", "ask", "echo(*)"),
        ("permission_request", "allow", None),
    ]
    permission = next(e for e in events if e.type == "permission_decision")
    data = json.loads(permission.data.model_dump_json())
    assert (data["decision"], data["source"]) == ("allow", "hook")


def test_before_tool_result_sees_only_executed_results() -> None:
    seen: list[str] = []

    async def deny(_call: ToolCallData, _ctx: RunContext[None]) -> ToolGate:
        return {"decision": "deny", "reason": "no"}

    async def guard(_c: ToolCallData, r: ToolResultData, _ctx: RunContext[None]) -> ResultGate:
        seen.append(r.origin)
        return {"decision": "proceed"}

    hooks: Hooks[None] = {"before_tool": deny, "before_tool_result": guard}
    _result, events = asyncio.run(run(hooks, [use(), text("ok")]))
    assert seen == []
    assert decisions(events) == [("before_tool", "deny")]


def test_before_tool_stops_at_the_first_deny_as_in_typescript() -> None:
    """Later extensions are not asked once one denies (before_tool and permission_request)."""
    asked: list[str] = []

    async def deny(_call: ToolCallData, _ctx: RunContext[None]) -> ToolGate:
        return {"decision": "deny", "reason": "no"}

    async def later(_call: ToolCallData, _ctx: RunContext[None]) -> ToolGate:
        asked.append("later")
        return {"decision": "allow"}

    async def main() -> list[tuple[str, str]]:
        echo = tool(name="echo", description="Echo.", input=Echo, runs="host", execute=Box().run)
        bot = agent(
            model=scripted_model({"responses": [use(), text("ok")]}),
            tools=[echo],
            permissions=ALLOW,
            extensions=[
                extension(name="first", hooks={"before_tool": deny}),
                extension(name="second", hooks={"before_tool": later}),
            ],
        )
        result = await bot.run("go", store=sqlite(":memory:"), deps=None)
        timeline = await result.thread.timeline()
        assert isinstance(timeline, Ok)
        return decisions([e.event for e in timeline.value.entries])

    assert asyncio.run(main()) == [("before_tool", "deny")]
    assert asked == []
