# pyright: strict
"""Summary compaction cases: the summary plus the kept tail
in the next request, a compacted run that continues from its recorded summary, a cutoff at the
step boundary before a tool pair, and a missing summary artifact that stops the request."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import NOW, obj, text, tokens
from .log import Log, reduce
from .pieces import (
    FINAL,
    READ_FILE,
    TAIL,
    answer,
    call,
    case,
    read_turn,
    render_case,
    result,
    started,
    user,
    write_case,
)
from .policies import CONTEXT, policy

if TYPE_CHECKING:
    import pathlib

    from .jcs import Obj

FAM = "context_compaction"
SUMMARY = "The user asked what README.md holds (one heading, demo)."


def build(root: pathlib.Path) -> None:
    _tail(root)
    _replay(root)
    _pairs(root)
    _missing(root)


def _compact(log: Log, first: Obj, last: Obj) -> Obj:
    """The recorded side request, its summary and compacted{threshold} over [first, last]."""
    side = log.model_request(compaction=True)
    log.model_response(side, [{"type": "text", "text": SUMMARY}], "end_turn", tokens(900, 40))
    return log.add(
        "compacted",
        {
            "from_seq": first["seq"],
            "to_seq": last["seq"],
            "from_event_id": first["event_id"],
            "to_event_id": last["event_id"],
            "summary_ref": log.art(SUMMARY.encode(), "text/plain"),
            "summary_request_event_id": side["event_id"],
            "trigger": "threshold",
        },
    )


def _two_turns() -> tuple[Log, Obj, Obj]:
    """A read turn (the range) and a short turn (the kept tail)."""
    log = Log()
    started(log, [READ_FILE], policy=policy(context=CONTEXT))
    read_turn(log)
    first, last = log.events[1], log.events[-1]
    user(log, "hi")
    answer(log, "Hello!")
    return log, first, last


def _tail(root: pathlib.Path) -> None:
    log, first, last = _two_turns()
    _compact(log, first, last)
    user(log, "Continue.")
    render_case(
        root,
        (
            "compaction-summary-tail",
            FAM,
            "A run over the compaction trigger appends compacted{range, summary_ref}; the "
            "events in the range stay in the log. The next request renders the summary inside "
            "the untrusted-reference wrapper in place of the range, then the kept tail and the "
            "new input.",
        ),
        log,
    )


def _replay(root: pathlib.Path) -> None:
    log, first, last = _two_turns()
    _compact(log, first, last)
    user(log, "Continue.")
    write_case(
        root,
        case(
            "compaction-replay-uses-recorded-summary",
            FAM,
            "recover",
            "A compacted run continues after a restart. The next request renders the recorded "
            "summary artifact; the estimate starts over after compacted, so nothing is "
            "summarized again: one turn request and its answer.",
            model_script="model.json",
        ),
        log,
        {"outcome": "ok", "state": reduce(log, NOW), "appended": TAIL},
        extra={"model.json": {"responses": [FINAL]}},
    )


def _pairs(root: pathlib.Path) -> None:
    log = Log()
    started(log, [READ_FILE], policy=policy(context=CONTEXT))
    read_turn(log)
    first, boundary = log.events[1], log.events[-1]
    user(log, "And NOTES.md?")
    call(log, "read_file", {"path": "NOTES.md"}, "call_2")
    result(log, "call_2", "# notes\n")
    answer(log, "It has one heading too.")
    _compact(log, first, boundary)
    user(log, "Continue.")
    render_case(
        root,
        (
            "compaction-keeps-tool-pairs",
            FAM,
            "The compacted range ends at the step boundary before the kept tail, never between "
            "a tool_call and its tool_result: call_2's call and result both render after the "
            "summary, so the request never holds half of a pair.",
        ),
        log,
    )


def _missing(root: pathlib.Path) -> None:
    log, first, last = _two_turns()
    compacted = _compact(log, first, last)
    del log.artifacts[text(obj(obj(compacted["data"])["summary_ref"])["sha256"])]
    user(log, "Continue.")
    write_case(
        root,
        case(
            "compaction-summary-missing-artifact",
            FAM,
            "render",
            "The summary artifact of a compacted event is missing on reload. The next request "
            "can't render the summary, so it is never sent without it: artifact_missing at the "
            "compacted event.",
        ),
        log,
        {"outcome": "error", "error": {"code": "artifact_missing", "seq": compacted["seq"]}},
    )
