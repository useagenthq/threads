# pyright: strict
"""Simulated-user eval conformance cases (lane 32): a case saved with `simulate` reruns offline
exactly as one without it, its stubs.json also holds the prefix's settled effects under
`scope: "prefix"`, and an unsettled prefix effect is recorded as `simulate_blocked`."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import sha
from .evals import MUST_LOOKUP, refund_turn, start
from .evals_turn import Turn
from .evals_write import passed, saved_case, write_eval
from .jcs import JsonValue, Obj, canonical

if TYPE_CHECKING:
    import pathlib

    from .log import Log

SIMULATE: Obj = {
    "kind": "model",
    "persona": "A customer who bought headphones 40 days ago. Polite but persistent.",
    "goal": "Get a refund, or a clear reason why not and what else is possible.",
    "max_messages": 3,
}
SCRIPT: Obj = {"kind": "script", "messages": ["It's order 1234.", "Then I'd like the repair."]}


def _hash(inp: Obj) -> str:
    return sha(canonical(inp))


def _stub(name: str, inp: Obj, occurrence: int, output: str, *, prefix: bool = False) -> Obj:
    out: Obj = {
        "tool": name,
        "args_hash": _hash(inp),
        "occurrence": occurrence,
        "output": output,
        "is_error": False,
    }
    if prefix:
        out["scope"] = "prefix"
    return out


def _earlier(log: Log, call: str, inp: Obj, output: str, text: str) -> None:
    """One completed turn before the saved one: a mediated call, settled, then an answer."""
    t = Turn(log)
    t.user(text)
    req = t.request()
    t.uses(req, [(call, "issue_refund", inp)])
    t.effect(call, "issue_refund", inp, output)
    t.say(f"Done: {output}.")
    t.done()


def _two_turns(log: Log, inp: Obj, output: str) -> Turn:
    """A prefix turn with its own effect, then the refund turn the case saves."""
    start(log, sandbox_provider="fake")
    _earlier(log, "p1", inp, output, "Where is order 7?")
    t = Turn(log)
    refund_turn(t)
    return t


def _unsettled(log: Log) -> Turn:
    """A prefix effect that ended unknown: begun, never committed or resolved."""
    start(log, sandbox_provider="fake")
    first = Turn(log)
    first.user("Refund order 7.")
    req = first.request()
    first.uses(req, [("p1", "issue_refund", {"id": "7"})])
    first.allow("p1")
    first.log.add("effect_begin", {"call_id": "p1", "attempt": 1})
    first.log.add("effect_unknown", {"call_id": "p1", "reason": "timeout"})
    first.result("p1", "the provider never answered", is_error=True)
    first.say("I can't tell whether that refund went through.")
    first.done()
    t = Turn(log)
    refund_turn(t)
    return t


def build(root: pathlib.Path) -> None:
    write_eval(root, "eval-simulate-offline", _offline)
    write_eval(root, "eval-simulate-prefix-stubs", _prefix_stubs)
    write_eval(root, "eval-simulate-unsettled-prefix", _unsettled_prefix)


def _refund_stub(occurrence: int = 0) -> Obj:
    return _stub("issue_refund", {"id": "42"}, occurrence, "refunded order 42")


def _offline(d: pathlib.Path) -> list[Obj]:
    """A simulated case and the same case without `simulate`: both pass offline, alike."""
    other: Obj = {"id": "7"}
    stubs: JsonValue = {
        "stubs": [
            _stub("issue_refund", other, 0, "refunded order 7", prefix=True),
            _refund_stub(),
        ]
    }
    saved_case(d, "plain", lambda log: _two_turns(log, other, "refunded order 7"), must=MUST_LOOKUP)
    saved_case(
        d,
        "simulated",
        lambda log: _two_turns(log, other, "refunded order 7"),
        must=MUST_LOOKUP,
        simulate=SIMULATE,
        files={"stubs.json": stubs},
    )
    return [passed("plain"), passed("simulated")]


def _prefix_stubs(d: pathlib.Path) -> list[Obj]:
    """The same call in the prefix and in the saved turn: occurrences resolve in log order."""
    same: Obj = {"id": "42"}
    stubs: JsonValue = {
        "stubs": [
            _stub("issue_refund", same, 0, "refunded order 42 the first time", prefix=True),
            _refund_stub(1),
        ]
    }
    saved_case(
        d,
        "repeat",
        lambda log: _two_turns(log, same, "refunded order 42 the first time"),
        must=MUST_LOOKUP,
        simulate=SCRIPT,
        files={"stubs.json": stubs},
    )
    return [passed("repeat")]


def _unsettled_prefix(d: pathlib.Path) -> list[Obj]:
    """An unsettled prefix effect is not stubbed; the case still reruns offline."""
    saved_case(
        d,
        "open-effect",
        _unsettled,
        must=MUST_LOOKUP,
        simulate=SIMULATE,
        simulate_blocked="unsettled_effect",
        files={"stubs.json": {"stubs": [_refund_stub()]}},
    )
    return [passed("open-effect")]
