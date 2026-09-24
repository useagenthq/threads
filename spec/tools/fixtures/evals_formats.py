# pyright: strict
"""Eval cases over what a case directory can hold (lane 22, A.2 and A.4): sandbox.json v2 and
v1, the not-runnable reasons saveCase records, framework tools, and cases an older Python
save_case wrote with no sandbox.json."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import sha, tool
from .evals import LOOKUP, MUST_LOOKUP, REFUND, first_turn, start
from .evals_turn import Turn
from .evals_write import passed, saved_case, skipped, write_eval
from .jcs import JsonValue, Obj, canonical
from .log import Log
from .memory import catalog_spec

if TYPE_CHECKING:
    import pathlib
    from collections.abc import Callable

PHOTO = tool("order_photo", "A photo of the parcel.", {"id": {"type": "string"}}, "read_only")
TODO = catalog_spec("todo_write", "read_only")
SEND = catalog_spec("send", "read_only")
SPAWN = catalog_spec("spawn_agent", "read_only")


def _same_tool_twice(log: Log) -> Turn:
    """Two lookup_order calls with different ids, then a result that is an image."""
    start(log, [LOOKUP, REFUND, PHOTO])
    t = Turn(log)
    t.user("Where are orders 41 and 42?")
    for call_id, order in (("c1", "41"), ("c2", "42")):
        req = t.request()
        t.uses(req, [(call_id, "lookup_order", {"id": order})])
        t.read(call_id, "lookup_order", {"id": order}, f"order {order}: in transit")
    req = t.request()
    t.uses(req, [("c3", "order_photo", {"id": "42"})])
    image = b"\x89PNG\r\n\x1a\nparcel-42"
    part: Obj = {
        "type": "image_ref",
        "ref": t.log.art(image, "image/png"),
        "width": 640,
        "height": 480,
    }
    t.read("c3", "order_photo", {"id": "42"}, "[image: parcel 42]", content=[part])
    t.say("Both are in transit; here is the parcel.")
    t.done()
    return t


def _v1(t: Turn) -> dict[str, JsonValue | bytes]:
    """A v1 sandbox.json, one preview per tool name (the first call's)."""
    tools: Obj = {}
    for r in t.results:
        if isinstance(r, dict) and isinstance(r["tool"], str) and r["tool"] not in tools:
            tools[r["tool"]] = {"output": r["preview"], "is_error": False}
    return {"sandbox.json": {"tools": tools}}


def _calls(name: str, spec: Obj, inp: Obj) -> Callable[[Log], Turn]:
    """A turn whose one call is `name`: its result is recorded, whatever it did."""

    def scenario(log: Log) -> Turn:
        start(log, [LOOKUP, REFUND, spec])
        t = Turn(log)
        t.user("Go.")
        req = t.request()
        t.uses(req, [("c1", name, inp)])
        t.decided("c1", "mode")
        t.result("c1", "ok")
        t.say("Done.")
        t.done()
        return t

    return scenario


def _todo(log: Log) -> Turn:
    """A turn whose only call is todo_write: the loop runs it from the log."""
    start(log, [LOOKUP, REFUND, TODO])
    t = Turn(log)
    t.user("Plan the refund.")
    req = t.request()
    todos: list[JsonValue] = [{"id": "1", "content": "Refund order 42", "status": "pending"}]
    t.uses(req, [("c1", "todo_write", {"todos": todos})])
    t.decided("c1", "mode")
    t.log.add("todos_updated", {"call_id": "c1", "todos": todos})
    t.result("c1", "ok")
    t.say("Planned.")
    t.done()
    return t


def _effect_only(log: Log) -> Turn:
    start(log)
    t = Turn(log)
    t.user("Refund order 42.")
    req = t.request()
    t.uses(req, [("c1", "issue_refund", {"id": "42"})])
    t.effect("c1", "issue_refund", {"id": "42"}, "refunded order 42")
    t.say("Refunded.")
    t.done()
    return t


def _added_later(log: Log) -> Turn:
    """lookup_order is back in the tool set by a tools_changed before the saved turn: removed,
    then re-added with its pinned spec (rule 17 allows no read-only tool the pin lacks)."""
    start(log, [REFUND, LOOKUP])
    removed: list[JsonValue] = [REFUND]
    readded: list[JsonValue] = [REFUND, LOOKUP]
    for tools in (removed, readded):
        log.add("tools_changed", {"tools": tools, "tools_hash": sha(canonical(tools))})
    t = Turn(log)
    t.user("Where is order 41?")
    req = t.request()
    t.uses(req, [("c1", "lookup_order", {"id": "41"})])
    t.read("c1", "lookup_order", {"id": "41"}, "order 41: shipped")
    t.say("Shipped.")
    t.done()
    return t


def _unknown_name(log: Log) -> Turn:
    """A call to a tool no set in effect names: it would need a recorded result."""
    t = _added_later(log)
    unknown: Obj = {"type": "tool_use", "call_id": "c9", "name": "mystery_tool", "input": {}}
    t.responses.insert(
        0,
        {
            "content": [unknown],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
    )
    return t


RESAVE = "offline_not_runnable:resave_case"


def build(root: pathlib.Path) -> None:
    write_eval(root, "eval-same-tool-twice", _same_tool)
    write_eval(root, "eval-team-calls", _team)
    write_eval(root, "eval-framework-tool-only", _framework_only)
    write_eval(root, "eval-child-threads", _child_threads)
    write_eval(root, "eval-fallback-tool-set-at-call", _tool_set_at_call)
    write_eval(root, "eval-python-no-sandbox-json", _no_sandbox_json)
    write_eval(root, "eval-extension-events-not-runnable", _extension_events)


def _same_tool(d: pathlib.Path) -> list[Obj]:
    saved_case(d, "v1", _same_tool_twice, must=MUST_LOOKUP, files=_v1(_same_tool_twice(_scratch())))
    saved_case(d, "v2", _same_tool_twice, must=MUST_LOOKUP)
    return [skipped("v1", RESAVE), passed("v2")]


def _team(d: pathlib.Path) -> list[Obj]:
    team_calls: Obj = {"runnable": False, "reason": "team_calls"}
    send = _calls("send", SEND, {"to": "researcher-1", "text": "Check order 42."})
    saved_case(d, "send", send, must=[{"type": "tool_call"}], offline=team_calls)
    saved_case(d, "send-old-python", send, must=[{"type": "tool_call"}], old_python=True)
    reason = "offline_not_runnable:team_calls"
    return [skipped("send", reason), skipped("send-old-python", reason)]


def _framework_only(d: pathlib.Path) -> list[Obj]:
    must: list[JsonValue] = [{"type": "todos_updated"}]
    saved_case(d, "saved", _todo, must=must)
    saved_case(d, "saved-old-python", _todo, must=must, old_python=True)
    return [passed("saved"), passed("saved-old-python")]


def _child_threads(d: pathlib.Path) -> list[Obj]:
    spawn = _calls("spawn_agent", SPAWN, {"agent": "reviewer", "prompt": "Review order 42."})
    child: Obj = {"runnable": False, "reason": "child_threads"}
    saved_case(d, "spawn", spawn, must=[{"type": "tool_call"}], offline=child)
    saved_case(d, "spawn-old-python", spawn, must=[{"type": "tool_call"}], old_python=True)
    reason = "offline_not_runnable:child_threads"
    return [skipped("spawn", reason), skipped("spawn-old-python", reason)]


def _tool_set_at_call(d: pathlib.Path) -> list[Obj]:
    saved_case(d, "added-by-tools-changed", _added_later, must=MUST_LOOKUP, old_python=True)
    saved_case(d, "unknown-name", _unknown_name, must=MUST_LOOKUP, old_python=True)
    return [skipped("added-by-tools-changed", RESAVE), skipped("unknown-name", RESAVE)]


def _no_sandbox_json(d: pathlib.Path) -> list[Obj]:
    must: list[JsonValue] = [{"type": "tool_call"}]
    saved_case(d, "effect-only", _effect_only, must=must, old_python=True)
    saved_case(d, "with-read", first_turn, must=MUST_LOOKUP, old_python=True)
    return [passed("effect-only"), skipped("with-read", RESAVE)]


def _extension_events(d: pathlib.Path) -> list[Obj]:
    offline: Obj = {"runnable": False, "reason": "extension_events", "types": ["x_audit_note"]}
    saved_case(d, "saved", first_turn, must=MUST_LOOKUP, offline=offline)
    return [skipped("saved", "offline_not_runnable:extension_events (x_audit_note)")]


def _scratch() -> Log:
    """A scratch build, read only for the records a scenario keeps."""
    return Log()
