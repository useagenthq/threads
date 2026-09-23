# pyright: strict
"""Model fallback across a switch hook and the next turn (ADR 0020): a denied switch keeps the
old epoch, a turn-scoped fallback reverts at the next input, and a thread-scoped one sticks."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import NOW, eid, num, obj
from .log import Log, reduce
from .pieces import FINAL, READ_FILE, answer, case, render_case, started, user, write_case
from .policies import PRIMARY_SETTINGS, RETRY, SMALL_SETTINGS, policy

if TYPE_CHECKING:
    import pathlib

    from .jcs import JsonValue, Obj

FAM = "cancellation_resume"
DENY: Obj = {"extension": "ops", "hook": "before_model_switch", "decision": "deny"}


def build(root: pathlib.Path) -> None:
    _denied(root)
    _reverts(root)
    _sticks(root)
    _deny_keyed(root)
    _early_decision(root)


def _overloaded(log: Log, attempt: int) -> Obj:
    """One attempt the provider refuses as overloaded (529)."""
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


def _two_overloaded(scope: str) -> Log:
    """A thread whose first turn got two overloaded answers: with fallback_after 2 the next
    step asks to switch to the small model."""
    log = Log()
    retry: Obj = {**RETRY, "fallback_scope": scope}
    started(log, [READ_FILE], policy=policy(retry=retry, fallback=[SMALL_SETTINGS]))
    user(log, "Say done.")
    _wait(log, _overloaded(log, 1), 1000)
    return log


def _denied(root: pathlib.Path) -> None:
    log = _two_overloaded("turn")
    second = _overloaded(log, 2)
    log.add("hook_decision", {**DENY, "reason": "stay on the primary"})
    _wait(log, second, 2000)
    render_case(
        root,
        (
            "model-fallback-switch-denied-retries",
            FAM,
            "before_model_switch denies the fallback: its hook_decision{deny} is recorded, "
            "there is no settings_changed, and the retry is scheduled on the old epoch, so the "
            "next request still declares the primary's line 0.",
        ),
        log,
    )


def _fell_back(scope: str, *, early: bool = False) -> tuple[Log, Obj, Obj]:
    """Turn 1 falls back to the small model, which answers; turn 2's input is recorded. Returns
    the log and the declared prefixes of the primary and the fallback epoch. `early` records,
    just before the input, a deny that names the input's event id: a decision before its input
    never settles it."""
    log = _two_overloaded(scope)
    second = _overloaded(log, 2)
    fallback: Obj = {
        "reason": "fallback",
        "settings": SMALL_SETTINGS,
        "cause_event_id": second["event_id"],
    }
    log.add("settings_changed", fallback)
    answer(log, "Done.")
    if early:
        log.add("hook_decision", {**DENY, "input_event_id": eid(log.seq + 2, log.branch)})
    user(log, "Say it again.")
    prefixes = [
        obj(e["data"])["declared_prefix"] for e in log.events if e["type"] == "model_request"
    ]
    return log, obj(prefixes[0]), obj(prefixes[-1])


def _next_turn(prefix: Obj) -> list[JsonValue]:
    """The turn the runner sends: one request declaring `prefix`, answered, then its end."""
    return [
        {"type": "model_request", "data": {"attempt": 1, "declared_prefix": prefix}},
        {"type": "model_response", "data": {"content": FINAL["content"]}},
        {"type": "turn_completed", "data": {"reason": "end_turn"}},
    ]


def _recover_case(
    root: pathlib.Path, name: str, desc: str, log: Log, appended: list[JsonValue]
) -> None:
    write_case(
        root,
        case(name, FAM, "recover", desc, model_script="model.json"),
        log,
        {"outcome": "ok", "state": reduce(log, NOW), "appended": appended},
        extra={"model.json": {"responses": [FINAL]}},
    )


def _reverts(root: pathlib.Path) -> None:
    log, primary, _ = _fell_back("turn")
    revert: Obj = {
        "reason": "revert",
        "settings": PRIMARY_SETTINGS,
        "cause_event_id": log.events[-1]["event_id"],
    }
    _recover_case(
        root,
        "model-fallback-reverts-next-input",
        "Turn 1 fell back to the small model. With fallback_scope turn, turn 2's user_input is "
        "followed by settings_changed{reason: revert} back to the primary settings, caused by "
        "that input, before its first model_request, which declares the primary's line 0.",
        log,
        [{"type": "settings_changed", "actor_kind": "host", "data": revert}, *_next_turn(primary)],
    )


def _sticks(root: pathlib.Path) -> None:
    log, _, small = _fell_back("thread")
    _recover_case(
        root,
        "model-fallback-thread-scope-sticks",
        "Turn 1 fell back to the small model. With fallback_scope thread nothing reverts: "
        "turn 2's request stays on the fallback epoch and declares its line 0.",
        log,
        _next_turn(small),
    )


def _deny_keyed(root: pathlib.Path) -> None:
    log, _, small = _fell_back("turn")
    decided: Obj = {
        **DENY,
        "reason": "stay on the fallback",
        "input_event_id": log.events[-1]["event_id"],
    }
    log.add("hook_decision", decided)
    _recover_case(
        root,
        "model-fallback-revert-deny-keyed-to-input",
        "Turn 2's input is recorded with a before_model_switch deny keyed to it (input_event_id), "
        "then the run stopped. Recovery never asks the hook again for that input and appends no "
        "revert: the turn is sent on the fallback epoch.",
        log,
        _next_turn(small),
    )


def _early_decision(root: pathlib.Path) -> None:
    log, primary, _ = _fell_back("turn", early=True)
    revert: Obj = {
        "reason": "revert",
        "settings": PRIMARY_SETTINGS,
        "cause_event_id": log.events[-1]["event_id"],
    }
    _recover_case(
        root,
        "model-fallback-revert-ignores-earlier-decision",
        "A before_model_switch deny names turn 2's input but was recorded before it. Only a "
        "decision after the input settles its revert, so recovery reverts to the primary as if "
        "there were none.",
        log,
        [{"type": "settings_changed", "actor_kind": "host", "data": revert}, *_next_turn(primary)],
    )
