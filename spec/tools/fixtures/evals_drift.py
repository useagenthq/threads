# pyright: strict
"""Eval drift cases (lane 22, B.3): each case's recorded config against a dry pin of the agent
as it is now (agents.json). What a dry pin can't see is unchecked, never stale."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import BRANCH, THREAD, eid, num, obj, tool
from .evals import LOOKUP, MUST_LOOKUP, REFUND, first_turn, pinned, refund_turn, start
from .evals_turn import Turn
from .evals_write import passed, saved_case, write_eval
from .evals_write import stale as stale_case
from .policies import RETRY, SMALL_SETTINGS, policy

if TYPE_CHECKING:
    import pathlib
    from collections.abc import Callable

    from .jcs import JsonValue, Obj
    from .log import Log

EXCHANGE = tool("issue_exchange", "Exchange an order.", {"id": {"type": "string"}}, "unguarded")
MCP = tool(
    "mcp__jira__create_issue", "Create a Jira issue.", {"title": {"type": "string"}}, "unguarded"
)
CRM: Obj = {
    **tool("crm__crm_lookup", "Look a customer up.", {"email": {"type": "string"}}, "read_only"),
    "origin": {"extension": "crm"},
}


def _deferred(props: Obj) -> Obj:
    return {**tool("export_orders", "Export orders.", props, "read_only"), "defer_loading": True}


def pin(started: Obj, **seen: list[JsonValue]) -> Obj:
    """agents.json's entry: a dry pin's thread_started and what it couldn't see."""
    return {
        "started": started,
        "mcp": seen.get("mcp", []),
        "setup_extensions": seen.get("setup_extensions", []),
        "setup_providers": seen.get("setup_providers", []),
    }


FAKE = pinned([LOOKUP, REFUND], sandbox_provider="fake")


def _drift_case(
    root: pathlib.Path,
    name: str,
    scenario: Callable[[Log], Turn],
    agents: list[JsonValue],
    outcome: Callable[[str], Obj],
) -> None:
    def cases(d: pathlib.Path) -> list[Obj]:
        saved_case(d, "refund", scenario, must=MUST_LOOKUP)
        return [outcome("refund")]

    write_eval(root, name, cases, agents)


def _with_tools(tools: list[JsonValue], **more: JsonValue) -> Callable[[Log], Turn]:
    def scenario(log: Log) -> Turn:
        start(log, tools, **more)
        t = Turn(log)
        refund_turn(t)
        return t

    return scenario


def ok(unchecked: list[JsonValue] | None = None) -> Callable[[str], Obj]:
    drift: Obj = {"ok": True, "kinds": []}
    if unchecked is not None:
        drift["unchecked"] = unchecked
    return lambda name: passed(name, drift=drift)


def build(root: pathlib.Path) -> None:
    _drift_case(root, "eval-no-drift", first_turn, [pin(FAKE)], ok())
    changed = pinned(
        [LOOKUP, REFUND, EXCHANGE],
        sandbox_provider="fake",
        instructions="You handle refunds and exchanges.",
        model={"provider": "scripted", "name": "scripted-2"},
    )
    drift: Obj = {
        "ok": False,
        "kinds": ["prompt", "tools", "model"],
        "tools": {"added": ["issue_exchange"], "removed": [], "changed": []},
    }
    _drift_case(
        root,
        "eval-drift",
        first_turn,
        [pin(changed)],
        lambda n: stale_case(n, "drift: prompt, tools (+issue_exchange), model", drift),
    )
    _drift_case(root, "eval-drift-after-fallback", _after_fallback, [pin(FALLBACK)], ok())
    _drift_case(
        root,
        "eval-drift-mcp-unchecked",
        _with_tools([LOOKUP, REFUND, MCP]),
        [pin(pinned([LOOKUP, REFUND]), mcp=["jira"])],
        ok(["mcp:jira"]),
    )
    _drift_case(root, "eval-drift-child-unchecked", _child, [pin(FAKE)], ok(["relation:spawn"]))
    _drift_case(
        root,
        "eval-drift-memory-setup-unchecked",
        first_turn,
        [pin(FAKE, setup_providers=["memory"])],
        ok(["memory"]),
    )
    _drift_case(
        root,
        "eval-drift-extension-setup-unchecked",
        _with_tools([LOOKUP, REFUND, CRM]),
        [pin(pinned([LOOKUP, REFUND]), setup_extensions=["crm"])],
        ok(["extension:crm"]),
    )
    _deferred_schema(root)


def _deferred_schema(root: pathlib.Path) -> None:
    before = _deferred({"since": {"type": "string"}})
    after = _deferred({"since": {"type": "string"}, "until": {"type": "string"}})
    drift: Obj = {
        "ok": False,
        "kinds": ["tools"],
        "tools": {"added": [], "removed": [], "changed": ["export_orders"]},
    }
    _drift_case(
        root,
        "eval-drift-deferred-schema",
        _with_tools([LOOKUP, REFUND, before]),
        [pin(pinned([LOOKUP, REFUND, after]))],
        lambda n: stale_case(n, "drift: tools (~export_orders)", drift),
    )


FALLBACK = pinned(
    [LOOKUP, REFUND],
    policy=policy(retry={**RETRY, "fallback_scope": "thread"}, fallback=[SMALL_SETTINGS]),
)


def _overloaded(log: Log, attempt: int) -> Obj:
    """One attempt the provider refuses as overloaded (529), as fallbacks.py records it."""
    r = log.model_request(attempt)
    data: Obj = {
        "request_event_id": r["event_id"],
        "provider_outcome": "failed",
        "reason": "overloaded",
        "http_status": 529,
    }
    return log.add("model_attempt_abandoned", data)


def _wait(log: Log, abandoned: Obj, delay: int) -> None:
    data: Obj = {
        "request_event_id": obj(abandoned["data"])["request_event_id"],
        "delay_ms": delay,
        "not_before": num(abandoned["time"]) + delay,
        "basis": "backoff",
    }
    log.add("retry_scheduled", data)


def _after_fallback(log: Log) -> Turn:
    """Turn 1 fell back to the small model for the thread; turn 2 runs on it and is saved."""
    log.add("thread_started", FALLBACK)
    first = Turn(log)
    first.user("Say done.")
    _wait(log, _overloaded(log, 1), 1000)
    second = _overloaded(log, 2)
    fallback: Obj = {
        "reason": "fallback",
        "settings": SMALL_SETTINGS,
        "cause_event_id": second["event_id"],
    }
    log.add("settings_changed", fallback)
    first.say("Done.")
    first.done()
    t = Turn(log)
    refund_turn(t)
    return t


def _child(log: Log) -> Turn:
    """A subagent's thread: its thread_started names the parent that spawned it."""
    parent: Obj = {
        "thread_id": THREAD.replace("0001", "0009"),
        "branch_id": BRANCH.replace("0001", "0009"),
        "event_id": eid(9, BRANCH.replace("0001", "0009")),
        "relation": "subagent",
    }
    start(log, sandbox_provider="fake", parent=parent)
    t = Turn(log)
    refund_turn(t)
    return t
