# pyright: strict
"""Eval hook cases (lane 22, A.2): a rerun loads no user code, so each extension is a stand-in
that answers from the turn's extensions.json records. Recorded hooks answer in order and fail
closed past their records; observation hooks answer only at the call their record names."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .evals import LOOKUP, MUST_LOOKUP, REFUND, first_turn, refund_turn, start
from .evals_turn import Turn
from .evals_write import RERUN_OK, passed, result, saved_case, write_eval
from .log import Log
from .memory import catalog_spec

if TYPE_CHECKING:
    import pathlib
    from collections.abc import Callable

    from .jcs import JsonValue, Obj

RECALL = catalog_spec("search_memory", "read_only")
MEMORY: Obj = {
    "source": "memory",
    "trust": "untrusted_reference",
    "origin": {"id": "m1", "version": "1"},
    "text": "Refunds are allowed within 30 days.",
}


def _lookup(t: Turn, call_id: str, order: str, ext: str | None = None) -> None:
    req = t.request()
    t.uses(req, [(call_id, "lookup_order", {"id": order})])
    if ext is not None:
        t.hook(ext, "before_tool", "allow", {"call_id": call_id})
    _read(t, call_id, order, hooked=ext is not None)


def _read(t: Turn, call_id: str, order: str, *, hooked: bool) -> None:
    if hooked:
        t.decided(call_id, "hook")
        t.result(call_id, f"order {order}: shipped")
        t.record_read(call_id, "lookup_order", {"id": order}, f"order {order}: shipped")
    else:
        t.read(call_id, "lookup_order", {"id": order}, f"order {order}: shipped")


def _recall(log: Log) -> Turn:
    start(log, [LOOKUP, REFUND, RECALL])
    t = Turn(log)
    t.user("What is the refund policy?")
    req = t.request()
    t.uses(req, [("c1", "search_memory", {"query": "refund policy"})])
    t.hook("audit", "before_tool", "allow", {"call_id": "c1"})
    t.decided("c1", "hook")
    preview = "1 memories, shown below as untrusted references"
    t.result("c1", preview)
    t.record_read("c1", "search_memory", {"query": "refund policy"}, preview)
    t.log.add("injected", MEMORY)
    t.recall.append({"source": "memory", "occurrence": 1, "items": [MEMORY]})
    t.say("Refunds are allowed within 30 days.")
    t.done()
    return t


def _guided(log: Log) -> Turn:
    """after_model guides the first answer; on_stop asks for one more pass, then stops."""
    start(log)
    t = Turn(log)
    t.user("Summarise the refund policy.")
    r1 = t.request()
    t.respond(r1, [{"type": "text", "text": "Draft."}], "end_turn")
    key1: Obj = {"request_event_id": r1["event_id"]}
    t.hook("style", "after_model", "guide", key1, "Cite the policy.")
    t.inject("style", "Cite the policy.", "trusted_instruction")
    r2 = t.request()
    t.respond(r2, [{"type": "text", "text": "Draft citing the policy."}], "end_turn")
    key2: Obj = {"request_event_id": r2["event_id"]}
    t.hook("style", "after_model", "proceed", key2)
    t.hook("style", "on_stop", "continue", key2, "Add the order id.")
    t.inject("style", "Add the order id.", "trusted_instruction")
    r3 = t.request()
    t.respond(r3, [{"type": "text", "text": "Final, for order 42."}], "end_turn")
    key3: Obj = {"request_event_id": r3["event_id"]}
    t.hook("style", "after_model", "proceed", key3)
    t.hook("style", "on_stop", "stop", key3)
    t.done()
    return t


def _two_hooked(log: Log) -> Turn:
    start(log)
    t = Turn(log)
    t.user("Check orders 41 and 42.")
    _lookup(t, "c1", "41", "audit")
    _lookup(t, "c2", "42", "audit")
    t.say("Both shipped.")
    t.done()
    return t


def _three_calls(annotate: str) -> Callable[[Log], Turn]:
    """Three reads, the last two in one response; after_tool annotates only the third."""

    def scenario(log: Log) -> Turn:
        start(log)
        t = Turn(log)
        t.user("Check orders 41, 42 and 43.")
        _lookup(t, "c1", "41")
        req = t.request()
        calls: list[tuple[str, str, Obj]] = [
            ("c2", "lookup_order", {"id": "42"}),
            ("c3", "lookup_order", {"id": "43"}),
        ]
        t.uses(req, calls, allow=True)
        for call_id, order in (("c2", "42"), ("c3", "43")):
            t.result(call_id, f"order {order}: shipped")
            t.record_read(call_id, "lookup_order", {"id": order}, f"order {order}: shipped")
        t.hook(
            "notes", "after_tool", "annotate", {"call_id": "c3"}, "checked", {"call_id": annotate}
        )
        t.say("All three shipped.")
        t.done()
        return t

    return scenario


def _ordered(log: Log) -> Turn:
    """alpha then beta at before_tool (declaration order), beta then alpha at after_tool."""
    start(log)
    t = Turn(log)
    t.user("Check order 41.")
    req = t.request()
    t.uses(req, [("c1", "lookup_order", {"id": "41"})])
    for ext in ("alpha", "beta"):
        t.hook(ext, "before_tool", "allow", {"call_id": "c1"})
    t.decided("c1", "hook")
    t.result("c1", "order 41: shipped")
    t.record_read("c1", "lookup_order", {"id": "41"}, "order 41: shipped")
    for ext in ("beta", "alpha"):
        t.hook(ext, "after_tool", "annotate", {"call_id": "c1"}, ext, {"call_id": "c1"})
    t.say("Shipped.")
    t.done()
    return t


def _session_start(log: Log) -> Turn:
    start(log)
    t = Turn(log)
    t.hook("boot", "session_start", "proceed")
    refund_turn(t)
    return t


def _gated_twice(log: Log) -> Turn:
    """before_model proceeds before each of the turn's two requests."""
    start(log)
    t = Turn(log)
    t.user("Check order 41.")
    t.hook("gate", "before_model", "proceed")
    _lookup(t, "c1", "41")
    t.hook("gate", "before_model", "proceed")
    t.say("Shipped.")
    t.done()
    return t


def _only_first_record(t: Turn) -> dict[str, JsonValue | bytes]:
    return {"extensions.json": {"hooks": t.hooks[:1], "recall": []}}


def _failed(reason: str, **rerun: JsonValue) -> Obj:
    return result(
        "saved",
        "failed",
        reason,
        {"replay": {"ok": True}, "rerun": {**RERUN_OK, "ok": False, **rerun}},
    )


def build(root: pathlib.Path) -> None:
    write_eval(root, "eval-hooks-recall-replayed", lambda d: _single(d, _recall))
    write_eval(root, "eval-observation-hook-no-record", lambda d: _single(d, first_turn))
    write_eval(root, "eval-hook-guide-injected", lambda d: _single(d, _guided, "turn_completed"))
    write_eval(root, "eval-stand-in-order", lambda d: _single(d, _ordered))
    write_eval(root, "eval-extension-only-after-tool", lambda d: _single(d, first_turn))
    write_eval(root, "eval-extension-only-session-start", lambda d: _single(d, _session_start))
    write_eval(root, "eval-observation-keyed-third-call", _keyed)
    write_eval(root, "eval-gating-hook-missing", _gating_missing)
    write_eval(root, "eval-recorded-hook-overrun", _overrun)


def _single(d: pathlib.Path, scenario: Callable[[Log], Turn], must: str = "tool_call") -> list[Obj]:
    saved_case(d, "saved", scenario, must=[{"type": must}])
    return [passed("saved")]


def _keyed(d: pathlib.Path) -> list[Obj]:
    saved_case(d, "keyed-c1", _three_calls("c1"), must=MUST_LOOKUP)
    saved_case(d, "keyed-c3", _three_calls("c3"), must=MUST_LOOKUP)
    mismatch: Obj = {"index": 6, "want": "model_request", "got": "hook_decision"}
    keyed_c1 = _failed("rerun: event 6 is hook_decision, recorded model_request", mismatch=mismatch)
    return [{**keyed_c1, "name": "keyed-c1"}, passed("keyed-c3")]


def _gating_missing(d: pathlib.Path) -> list[Obj]:
    turn = _two_hooked
    saved_case(d, "saved", turn, must=MUST_LOOKUP, files=_only_first_record(turn(_fresh())))
    mismatch: Obj = {"index": 10, "want": "hook_decision", "got": "hook_decision"}
    return [_failed("rerun: unrecorded_hook", unrecorded_hooks=1, mismatch=mismatch)]


def _overrun(d: pathlib.Path) -> list[Obj]:
    turn = _gated_twice
    saved_case(d, "saved", turn, must=MUST_LOOKUP, files=_only_first_record(turn(_fresh())))
    mismatch: Obj = {"index": 7, "want": "hook_decision", "got": "hook_decision"}
    return [_failed("rerun: unrecorded_hook", script_left=1, unrecorded_hooks=1, mismatch=mismatch)]


def _fresh() -> Log:
    """A scratch log: the records a scenario would write, to trim for extensions.json."""
    return Log()
