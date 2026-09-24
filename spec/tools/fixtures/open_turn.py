# pyright: strict
"""Recovery of an open turn with nothing pending: close it as interrupted,
unless none of its input has been sent yet, in which case the run continues it."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import ALICE, NOW, arr, obj
from .log import Log, reduce
from .pieces import FINAL, NO_MODEL, READ_FILE, TAIL, case, started, user, write_case

if TYPE_CHECKING:
    import pathlib


def build(root: pathlib.Path) -> None:
    # A final answer was recorded, then the process died before turn_completed.
    log = Log()
    started(log, [READ_FILE])
    user(log, "Say hello.")
    r = log.model_request()
    log.model_response(r, arr(FINAL["content"]), "end_turn", obj(FINAL["usage"]))
    write_case(
        root,
        case(
            "recover-open-turn-answered-interrupted",
            "cancellation_resume",
            "recover",
            "Crash after a model_response and before turn_completed. Nothing is pending and the "
            "turn's input was already sent, so recovery closes the turn as interrupted instead "
            "of guessing how it would have ended. Nothing is sent to the model.",
            NOW,
            model_script="model.json",
        ),
        log,
        {
            "outcome": "ok",
            "state": reduce(log, NOW),
            "appended": [
                {
                    "type": "turn_completed",
                    "actor_kind": "recovery",
                    "epoch": 2,
                    "data": {"reason": "interrupted"},
                }
            ],
        },
        extra={"model.json": NO_MODEL},
    )

    # A final answer was recorded and the turn was cancelled, then the process died.
    log = Log()
    started(log, [READ_FILE])
    user(log, "Say hello.")
    r = log.model_request()
    log.model_response(r, arr(FINAL["content"]), "end_turn", obj(FINAL["usage"]))
    cr = log.add("cancel_requested", {"scope": "turn"}, actor="user", principal=ALICE)
    write_case(
        root,
        case(
            "recover-open-turn-cancel-requested-cancels",
            "cancellation_resume",
            "recover",
            "Crash after a model_response and a cancel_requested, before turn_completed. The "
            "cancel is durable, so recovery doesn't close the turn as interrupted: the run carries "
            "the cancel out and the turn ends cancelled. Nothing is sent to the model.",
            NOW,
            model_script="model.json",
        ),
        log,
        {
            "outcome": "ok",
            "state": reduce(log, NOW),
            "appended": [
                {"type": "cancelled", "data": {"request_event_id": cr["event_id"]}},
                {"type": "turn_completed", "data": {"reason": "cancelled"}},
            ],
        },
        extra={"model.json": NO_MODEL},
    )

    # The input was recorded, then the process died before any model_request.
    log = Log()
    started(log, [READ_FILE])
    user(log, "Say hello.")
    write_case(
        root,
        case(
            "recover-open-turn-unsent-continues",
            "cancellation_resume",
            "recover",
            "Crash right after user_input, before any model_request. None of the turn's input "
            "has been sent, so recovery appends nothing and leaves the turn open; the run then "
            "continues it normally and the turn ends end_turn.",
            NOW,
            model_script="model.json",
        ),
        log,
        {"outcome": "ok", "state": reduce(log, NOW), "appended": TAIL},
        extra={"model.json": {"responses": [FINAL]}},
    )
