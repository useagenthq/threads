# pyright: strict
"""vectors/drift.json (lane 22, B.3): a recorded thread_started and line 0 against dry pins,
and the drift check both runtimes must return."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import tool
from .evals import LOOKUP, REFUND, pinned
from .evals_drift import pin
from .log import Log
from .render import render

if TYPE_CHECKING:
    from .jcs import JsonValue, Obj

EXCHANGE = tool("issue_exchange", "Exchange an order.", {"id": {"type": "string"}}, "unguarded")
MCP = tool(
    "mcp__jira__create_issue", "Create a Jira issue.", {"title": {"type": "string"}}, "unguarded"
)
CRM: Obj = {
    **tool("crm__crm_lookup", "Look a customer up.", {"email": {"type": "string"}}, "read_only"),
    "origin": {"extension": "crm"},
}
CRM_OLD = tool("crm__crm_lookup", "Look a customer up.", {"email": {"type": "string"}}, "read_only")
BASE = pinned([LOOKUP, REFUND])
OK: Obj = {"ok": True, "kinds": []}


def _line0(started: Obj) -> str:
    log = Log()
    log.add("thread_started", started)
    _, line0 = render(log.events, log.artifacts, None, None)
    return line0.decode().rstrip("\n")


def _tools(added: list[JsonValue], removed: list[JsonValue], changed: list[JsonValue]) -> Obj:
    return {"added": added, "removed": removed, "changed": changed}


def _stale(kinds: list[JsonValue], **more: JsonValue) -> Obj:
    return {"ok": False, "kinds": kinds, **more}


def _case(
    name: str, started: Obj, pins: list[JsonValue], expect: Obj, *, line0: bool = True
) -> JsonValue:
    recorded: Obj = {"started": started}
    if line0:
        recorded["line0"] = _line0(started)
    return {"name": name, "recorded": recorded, "pins": pins, "expect": expect}


def drift_vector() -> list[JsonValue]:
    params: Obj = {"max_tokens": 2048}
    return [
        _case("the same config", BASE, [pin(BASE)], OK),
        _case(
            "changed instructions",
            BASE,
            [pin(pinned([LOOKUP, REFUND], instructions="You handle exchanges."))],
            _stale(["prompt"]),
        ),
        _case(
            "a tool added, one removed",
            BASE,
            [pin(pinned([LOOKUP, EXCHANGE]))],
            _stale(["tools"], tools=_tools(["issue_exchange"], ["issue_refund"], [])),
        ),
        _case(
            "a changed effect class",
            BASE,
            [pin(pinned([LOOKUP, {**REFUND, "effect_class": "reconcilable"}]))],
            _stale(["tools"], tools=_tools([], [], ["issue_refund"])),
        ),
        _case(
            "another model",
            BASE,
            [pin(pinned([LOOKUP, REFUND], model={"provider": "scripted", "name": "scripted-2"}))],
            _stale(["model"]),
        ),
        _case(
            "other params",
            BASE,
            [pin(pinned([LOOKUP, REFUND], model_params=params))],
            _stale(["model"]),
        ),
        _case(
            "only a hashed setting",
            BASE,
            [pin({**BASE, "config_hash": "0" * 64})],
            _stale(["config"]),
        ),
        _case(
            "an MCP server's tools are unchecked",
            pinned([LOOKUP, REFUND, MCP]),
            [pin(BASE, mcp=["jira"])],
            {**OK, "unchecked": ["mcp:jira"]},
        ),
        _case(
            "a setup-bearing extension: its tools and the prompt are unchecked",
            pinned([LOOKUP, REFUND, CRM]),
            [pin(pinned([LOOKUP, REFUND], instructions="Also: CRM."), setup_extensions=["crm"])],
            {**OK, "unchecked": ["extension:crm"]},
        ),
        _case(
            "a case saved before tool origins: its tools are unchecked",
            pinned([LOOKUP, REFUND, CRM_OLD]),
            [pin(pinned([LOOKUP]), setup_extensions=["crm"])],
            {**OK, "unchecked": ["extension:crm"]},
        ),
        _case(
            "a setup-bearing memory provider: the prompt is unchecked",
            BASE,
            [pin(pinned([LOOKUP, REFUND], instructions="Remember."), setup_providers=["memory"])],
            {**OK, "unchecked": ["memory"]},
        ),
        _case(
            "a handoff target is unchecked",
            pinned(
                [LOOKUP, REFUND],
                parent={
                    "thread_id": "0192a000-0000-7000-8000-000000000009",
                    "branch_id": "0192b000-0000-7000-8000-000000000009",
                    "event_id": "0192e000-0000-7000-8000-000000000009",
                    "relation": "handoff",
                },
            ),
            [pin(BASE)],
            {**OK, "unchecked": ["relation:handoff"]},
        ),
        _case(
            "the agent is not given",
            BASE,
            [pin(pinned([LOOKUP], agent_name="billing"))],
            {"ok": False, "kinds": [], "agents": ["billing"]},
        ),
        _case(
            "no recorded prefix: the prompt is unchecked, the model still compared",
            BASE,
            [pin(pinned([LOOKUP, REFUND], model_params=params))],
            _stale(["model"], unchecked=["no_recorded_prefix"]),
            line0=False,
        ),
    ]
