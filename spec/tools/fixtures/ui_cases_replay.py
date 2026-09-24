# pyright: strict
"""AG-UI replay `ui` cases (spec/schema/ui/README.md, "Replays and the snapshot"): a retry or
the stream after a resume opens with MESSAGES_SNAPSHOT of the history, then the open-state
preamble, then the frames after the snapshot point."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import sha
from .jcs import canonical
from .ui_pieces import (
    CLIENT_IDS,
    EXPIRED,
    LATER,
    asked,
    called,
    decided,
    end,
    respond,
    result,
    say,
    ui_case,
    ui_log,
    use,
    user,
)

if TYPE_CHECKING:
    import pathlib

    from .jcs import JsonValue, Obj
    from .log import Log

READ: Obj = {"path": "README.md"}
CH_A = "0192c000-0000-7000-8000-0000000000e1"
CH_B = "0192c000-0000-7000-8000-0000000000e2"


def build(root: pathlib.Path) -> None:
    _client_ids(root)
    _fold(root)
    _open_step(root)
    _after_interrupt(root)
    _settled(root)


def _replay(
    root: pathlib.Path, name: str, desc: str, log: Log, run: Obj, **more: JsonValue
) -> None:
    inp: Obj = {"protocol": "ag-ui", "run_id": run["event_id"], "ids": CLIENT_IDS, "replay": True}
    ui_case(root, name, desc, log, {**inp, **more})


def _two_runs(first_id: str | None) -> tuple[Log, Obj, Obj]:
    log = ui_log()
    first = user(log, "Hi.", first_id)
    respond(log, [say("Hello!")])
    end(log)
    second = user(log, "What is in README.md?")
    respond(log, [say("One heading.")])
    end(log)
    return log, first, second


def _client_ids(root: pathlib.Path) -> None:
    log, _, second = _two_runs("msg-abc")
    _replay(
        root,
        "ui-snapshot-client-ids-ag-ui",
        "The snapshot names a UI-started run's user message by its client_message_id "
        "(msg-abc) and an API-started run's by its event_id, each before its answer.",
        log,
        second,
    )
    log, first, second = _two_runs(None)
    _replay(
        root,
        "ui-snapshot-receipt-fallback-ag-ui",
        "A run recorded without client_message_id is named by its ui receipt's message id.",
        log,
        second,
        receipts={str(first["event_id"]): "msg-old"},
    )


def _fold(root: pathlib.Path) -> None:
    log = ui_log()
    run = user(log, "Check both files.")
    other: Obj = {"path": "NOTES.md"}
    content: list[JsonValue] = [
        say("Checking."),
        use("call_1", "read_file", READ),
        use("call_2", "read_file", other),
        say("Both requested."),
    ]
    r, _ = respond(log, content, "tool_use")
    called(log, r, use("call_1", "read_file", READ))
    called(log, r, use("call_2", "read_file", other))
    result(log, "call_1", "# demo\n")
    result(log, "call_2", "notes\n")
    thought = log.art(b'{"type":"thinking"}', "application/json")
    reasoning: Obj = {
        "type": "reasoning",
        "provider": "scripted",
        "model": "scripted-1",
        "format": "thinking",
        "ref": thought,
        "summary": "Both look fine.",
    }
    respond(log, [reasoning, say("Both fine.")])
    end(log)
    _replay(
        root,
        "ui-snapshot-fold-ag-ui",
        "Two tool calls in one response, text after them, results and a reasoning summary: "
        "the snapshot is the stock client's own fold, one assistant message holding both calls, "
        "the results right after it, and the reasoning message.",
        log,
        run,
    )


def _open_step(root: pathlib.Path) -> None:
    log = ui_log()
    run = user(log, "What is in README.md?")
    r, _ = respond(log, [say("Looking."), use("call_1", "read_file", READ)], "tool_use")
    called(log, r, use("call_1", "read_file", READ))
    result(log, "call_1", "# demo\n")
    second = log.model_request()
    log.model_response(
        second, [say("One heading.")], "end_turn", {"input_tokens": 9, "output_tokens": 2}
    )
    end(log)
    live: list[JsonValue] = [
        {"register": True},
        {"commit": second["seq"]},
        {"commit": log.events[-1]["seq"]},
    ]
    _replay(
        root,
        "ui-replay-snapshot-ag-ui",
        "A replay from the head after a text, a tool call and its result, with a second model "
        "step open: RUN_STARTED, the snapshot, the STEP_STARTED preamble, then the later frames.",
        log,
        run,
        live=live,
    )


def _after_interrupt(root: pathlib.Path) -> None:
    log = ui_log()
    run = user(log, "Clean the build folder.")
    binding = asked(log, "call_1", CH_A, "rm -rf build")
    decided(log, binding, grant=True)
    resumed = log.events[-1]
    result(log, "call_1", "removed")
    respond(log, [say("Cleaned.")])
    end(log)
    live: list[JsonValue] = [
        {"register": True},
        {"commit": resumed["seq"]},
        {"commit": log.events[-1]["seq"]},
    ]
    _replay(
        root,
        "ui-resumed-after-interrupt-ag-ui",
        "The stream after a resume that granted the interrupt: a new AG-UI run opening with the "
        "snapshot through the grant, then the resumed run to its end.",
        log,
        run,
        live=live,
    )


def _settled(root: pathlib.Path) -> None:
    log = ui_log()
    run = user(log, "Clean both folders.")
    a: Obj = {"command": "rm -rf build"}
    b: Obj = {"command": "rm -rf dist"}
    r, _ = respond(log, [use("call_1", "run_shell", a), use("call_2", "run_shell", b)], "tool_use")
    del r
    binding_a = asked_existing(log, "call_1", CH_A, a)
    asked_existing(log, "call_2", CH_B, b, expires=EXPIRED)
    decided(log, binding_a, grant=True)
    result(log, "call_1", "removed")
    resume: list[JsonValue] = [
        {"interruptId": CH_A, "status": "resolved", "payload": {"decision": "deny"}},
        {"interruptId": CH_B, "status": "resolved", "payload": {"decision": "grant"}},
    ]
    _replay(
        root,
        "ui-resume-settled-ag-ui",
        "A resume for an interrupt another approver already granted, and one for an expired "
        "approval: nothing is recorded, one threads.resume_conflict per entry follows the "
        "snapshot, and RUN_FINISHED lists the interrupts the run is still parked on.",
        log,
        run,
        resume=resume,
    )


def asked_existing(
    log: Log, call_id: str, challenge: str, inp: Obj, expires: int | None = None
) -> Obj:
    """An approval park for a call already in the last response."""
    req = [e for e in log.events if e["type"] == "model_request"][-1]
    called(log, req, use(call_id, "run_shell", inp), allow=False)
    binding: Obj = {"challenge_id": challenge, "call_id": call_id, "args_hash": sha(canonical(inp))}
    log.add("approval_requested", {**binding, "expires_at": expires or LATER})
    log.add(
        "parked", {"address": {"kind": "approval", "id": challenge}, "reason": "awaiting_approval"}
    )
    return binding
