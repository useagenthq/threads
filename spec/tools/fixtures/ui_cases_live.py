# pyright: strict
"""`ui` cases for live text, open state at the end, cursors and AG-UI replays
(spec/schema/ui/README.md, "Live text", "Cursors", "Replays and the snapshot")."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import ALICE, tokens
from .ui_cases_replay import build as replays
from .ui_pieces import (
    CHILD,
    asked,
    both,
    called,
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


def build(root: pathlib.Path) -> None:
    _live(root)
    _open_at_end(root)
    _cursors(root)
    _replays(root)


def _commit(e: Obj) -> Obj:
    return {"commit": e["seq"]}


def _delta(req: Obj, part: int, text: str) -> Obj:
    return {"delta": {"request_event_id": req["event_id"], "part": part, "text": text}}


REGISTER: Obj = {"register": True}


def _live_log() -> tuple[Log, Obj, Obj]:
    """text, tool_use, text in one response; returns the log, the run and the request."""
    log = ui_log()
    run = user(log, "Read README.md and tell me.")
    content: list[JsonValue] = [say("Hello there."), use("call_1", "read_file", READ), say("Done.")]
    r, _ = respond(log, content, "tool_use")
    called(log, r, use("call_1", "read_file", READ))
    result(log, "call_1", "# demo\n")
    respond(log, [say("One heading.")])
    end(log)
    return log, run, r


def _last(log: Log) -> Obj:
    return {"commit": log.events[-1]["seq"]}


def _live(root: pathlib.Path) -> None:
    log, run, r = _live_log()
    rid: Obj = {"run_id": run["event_id"]}
    steps: dict[str, tuple[str, list[JsonValue]]] = {
        "ui-live-prefix": (
            "Deltas for parts 0 and 2 of a response of text, tool_use, text: part 0 streams "
            "live and its commit sends only the rest; part 2 arrives at commit, since part 1 "
            "did not stream (the order rule). One start and one end per part id.",
            [
                _commit(r),
                REGISTER,
                _delta(r, 0, "Hello "),
                _delta(r, 0, "there."),
                _delta(r, 2, "Do"),
                _delta(r, 2, "ne."),
                _last(log),
            ],
        ),
        "ui-live-joined-after-part0": (
            "A connection registered after part 0's first delta streams nothing of that "
            "request live, not even part 2.",
            [
                _commit(r),
                _delta(r, 0, "Hello "),
                REGISTER,
                _delta(r, 0, "there."),
                _delta(r, 2, "Done."),
                _last(log),
            ],
        ),
        "ui-live-race": (
            "A part whose first delta went out before registration: a delta between the "
            "registration and the head read, and one after the head read, are not taken for a "
            "first delta; the part arrives whole at commit.",
            [
                _commit(r),
                _delta(r, 0, "Hel"),
                REGISTER,
                _delta(r, 0, "lo "),
                _commit(r),
                _delta(r, 0, "there."),
                _last(log),
            ],
        ),
        "ui-live-joined-mid-part": (
            "A connection that registers mid-part gets no live frames for it and the whole "
            "committed part.",
            [
                _commit(r),
                _delta(r, 0, "Hello "),
                _delta(r, 0, "the"),
                REGISTER,
                _commit(r),
                _delta(r, 0, "re."),
                _last(log),
            ],
        ),
    }
    for name, (desc, live) in steps.items():
        both(root, name, desc, log, {**rid, "live": live})
    _mismatch(root)
    _abandoned(root)


def _mismatch(root: pathlib.Path) -> None:
    log = ui_log()
    run = user(log, "Read README.md.")
    r, _ = respond(log, [use("call_1", "read_file", READ)], "tool_use")
    called(log, r, use("call_1", "read_file", READ))
    result(log, "call_1", "# demo\n")
    respond(log, [say("One heading.")])
    end(log)
    live: list[JsonValue] = [_commit(r), REGISTER, _delta(r, 0, "Let me"), _last(log)]
    both(
        root,
        "ui-live-index-mismatch",
        "An adapter bug: a live part commits as a tool call. The connection sends text-end and "
        "closes without its closing frames (and without [DONE]); a replay gives the message.",
        log,
        {"run_id": run["event_id"], "live": live},
    )


def _abandoned(root: pathlib.Path) -> None:
    log = ui_log()
    run = user(log, "Say hi.")
    r = log.model_request()
    gone = log.add(
        "model_attempt_abandoned",
        {
            "request_event_id": r["event_id"],
            "provider_outcome": "unknown",
            "reason": "stream_broken",
        },
    )
    r2 = log.model_request(2)
    log.model_response(r2, [say("Hi.")], "end_turn", tokens(10, 2))
    end(log)
    live: list[JsonValue] = [_commit(r), REGISTER, _delta(r, 0, "Hel"), _commit(gone), _last(log)]
    both(
        root,
        "ui-live-abandoned",
        "Live text, then model_attempt_abandoned: text-end, the abandonment frames and the "
        "step's end; the resent attempt's step and parts use its new request id.",
        log,
        {"run_id": run["event_id"], "live": live},
    )


def _open_at_end(root: pathlib.Path) -> None:
    log = ui_log()
    run = user(log, "Say hi.")
    log.model_request()
    cr = log.add("cancel_requested", {"scope": "thread"}, actor="user", principal=ALICE)
    log.add("cancelled", {"request_event_id": cr["event_id"]})
    end(log, "cancelled")
    both(
        root,
        "ui-open-step-at-end-cancelled",
        "A run cancelled mid-call: the open model step closes before the closing frames.",
        log,
        {"run_id": run["event_id"]},
    )
    log = ui_log()
    run = user(log, "Say hi.")
    log.model_request()
    end(log, "error")
    both(
        root,
        "ui-open-step-at-end-error",
        "A run that failed mid-call: the open model step closes before the error.",
        log,
        {"run_id": run["event_id"]},
    )
    for how in ("parked", "cancelled", "error"):
        log, run = _background()
        _ends(log, how)
        both(
            root,
            f"ui-open-subagent-at-end-{how}",
            "A background legacy subagent still running when the run ends: suspended on a "
            "park, stopped (SUBAGENT_ERROR) on a cancel or an error, before RUN_FINISHED / "
            "RUN_ERROR.",
            log,
            {"run_id": run["event_id"]},
        )


def _background() -> tuple[Log, Obj]:
    log = ui_log()
    run = user(log, "Scan in the background, then clean up.")
    inp: Obj = {"agent": "scanner", "prompt": "Scan deps."}
    r, _ = respond(log, [use("call_1", "spawn_agent", inp)], "tool_use")
    called(log, r, use("call_1", "spawn_agent", inp))
    spawned: Obj = {
        "call_id": "call_1",
        "child_thread_id": CHILD,
        "agent_name": "scanner",
        "mode": "background",
        "isolation": "shared_sandbox",
    }
    log.add("agent_spawned", spawned)
    result(log, "call_1", "scanner started", origin="deferred")
    return log, run


def _ends(log: Log, how: str) -> None:
    if how == "parked":
        asked(log, "call_2", "0192c000-0000-7000-8000-0000000000b1", "rm -rf build")
    elif how == "cancelled":
        cr = log.add("cancel_requested", {"scope": "thread"}, actor="user", principal=ALICE)
        log.add("cancelled", {"request_event_id": cr["event_id"]})
        end(log, "cancelled")
    else:
        respond(log, [say("Oops.")])
        end(log, "error")


def _cursors(root: pathlib.Path) -> None:
    log = ui_log()
    run = user(log, "What is in README.md?")
    r, _ = respond(log, [use("call_1", "read_file", READ)], "tool_use")
    called(log, r, use("call_1", "read_file", READ))
    result(log, "call_1", "# demo\n")
    _, last = respond(log, [say("One heading: demo.")])
    end(log)
    seq = last["seq"]
    for k in range(4):
        ui_case(
            root,
            f"ui-resume-mid-event-ai-sdk-k{k}",
            f"The cursor {seq}:{k} of a four-frame event resumes with exactly frames {k + 1}.. "
            "of that event, then the events after it: no gap, no duplicate.",
            log,
            {"protocol": "ai-sdk", "run_id": run["event_id"], "after": f"{seq}:{k}"},
        )
    streams = {
        b"".join(
            ui_case(
                root,
                f"ui-cursor-snapshot-ag-ui-k{k}",
                f"AG-UI resumes at the event boundary whatever k is: the cursor {seq}:{k} gives "
                "RUN_STARTED, a snapshot through the event, the preamble, then the events after "
                "it, the same stream for every k.",
                log,
                {"protocol": "ag-ui", "run_id": run["event_id"], "after": f"{seq}:{k}"},
            )
        )
        for k in range(4)
    }
    if len(streams) != 1:
        raise AssertionError("an AG-UI cursor resumes at the event boundary")


def _replays(root: pathlib.Path) -> None:
    replays(root)
