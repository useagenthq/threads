# pyright: strict
"""Log integrity cases: reduce, torn tail, unknown events, chain and head."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import ALICE, BRANCH, CHILD, NOW, aref, eid, num
from .jcs import canonical
from .log import Log, reduce
from .pieces import (
    READ_FILE,
    base_simple,
    case,
    negative,
    read_turn,
    snapshot,
    started,
    user,
    write_case,
)

if TYPE_CHECKING:
    import pathlib


FORMAT_VERSION = b',"format_version":1'
# (name suffix, replacement bytes, expected code). Admission reads format and format_version
# before the strict schema: a newer integer version means a newer writer, anything else is junk.
VERSIONS = (
    ("2", b',"format_version":2', "unsupported_format"),
    ("missing", b"", "invalid_line"),
    ("string", b',"format_version":"1"', "invalid_line"),
    ("fraction", b',"format_version":1.5', "invalid_line"),
    ("zero", b',"format_version":0', "invalid_line"),
)


def _admission(root: pathlib.Path) -> None:
    for suffix, version, code in VERSIONS:
        for line in ("header", "head"):
            log = Log()
            if line == "header":
                log.lines[0] = log.lines[0].replace(FORMAT_VERSION, version)
            started(log, [READ_FILE])
            read_turn(log)
            head = log.head()
            if line == "head":
                head = head.replace(FORMAT_VERSION, version)
            seq = 0 if line == "header" else log.seq
            negative(
                root,
                f"{line}-format-version-{suffix}",
                f"The {line} line's format_version is {suffix}. A known format with an integer "
                "format_version above 1 is unsupported_format (the reader is too old, not "
                "corruption); a missing, non-integer or non-positive version is invalid_line. "
                "This is checked before the line's schema.",
                log,
                (code, seq, log.body() + head + b"\n"),
            )
    log = Log()
    log.lines[0] = log.lines[0].replace(b'"format":"threads.log"', b'"format":"threads.other"')
    started(log, [READ_FILE])
    read_turn(log)
    negative(
        root,
        "header-format-unknown",
        "The header's format is not threads.log: invalid_line, not unsupported_format.",
        log,
        ("invalid_line", 0),
    )


def build(root: pathlib.Path) -> None:
    _admission(root)
    # reduce-simple-run
    log = base_simple()
    write_case(
        root,
        case(
            "reduce-simple-run",
            "log",
            "reduce",
            "One completed turn with a read-only tool call. Reduce must produce the pinned "
            "state; read-only tools write no effect events.",
        ),
        log,
        {"outcome": "ok", "state": reduce(log, NOW)},
    )

    # torn-tail-truncated (JSONL import only; the SQLite store cannot tear)
    log = base_simple()
    good = log.body()
    state = reduce(log, NOW)
    nxt = user(log, "A second question that never finished writing.")
    torn = canonical(nxt)[:57]
    log.lines.pop()
    log.events.pop()
    write_case(
        root,
        case(
            "torn-tail-truncated",
            "log",
            "recover",
            "An export whose last line is a partial write with no newline and no head "
            "checkpoint. Import serves the valid prefix, never edits the source file, flags the "
            "head as unverified, keeps the dropped bytes as an artifact, and the imported "
            "branch records log_repaired (critical: false). Nothing else is appended because "
            "the prefix is balanced.",
        ),
        log,
        {
            "outcome": "ok",
            "state": state,
            "committed_bytes": len(good),
            "head_verified": False,
            "appended": [
                {
                    "type": "log_repaired",
                    "critical": False,
                    "epoch": 2,
                    "data": {
                        "truncated_bytes": len(torn),
                        "at_offset": len(good),
                        "dropped_ref": aref(torn, "application/octet-stream"),
                    },
                }
            ],
        },
        extra={"log.jsonl": good + torn},
    )

    # unknown-critical-event-refuses
    log = Log()
    started(log, [READ_FILE])
    user(log, "hello")
    log.add("telemetry_ping", {"note": "unknown and ignorable"}, critical=False)
    bad = log.add("approval_quorum", {"required": 2}, critical=True)
    negative(
        root,
        "unknown-critical-event-refuses",
        "An unknown non-critical event is skipped; the next unknown critical event refuses the "
        "whole log as unsupported (a newer writer), not as corruption.",
        log,
        ("unsupported_critical_event", num(bad["seq"])),
    )

    # chain / head integrity
    log = base_simple()
    lines = log.body().split(b"\n")
    lines[2] = lines[2].replace(b'"What is in README.md?"', b'"What is in LICENSE?"')
    negative(
        root,
        "chain-middle-edit-detected",
        "A byte edit to an event in the middle of the log (seq 2). The next line's prev_hash no "
        "longer matches: prev_hash_mismatch at seq 3.",
        log,
        ("prev_hash_mismatch", 3, b"\n".join(lines) + log.head() + b"\n"),
    )

    log = base_simple()
    cut = log.copy()
    cut.lines = log.lines[:-1]
    negative(
        root,
        "head-checkpoint-suffix-removed",
        "The last event was removed and every remaining line still chains. Only the head "
        "checkpoint (seq 10) reveals it: head_mismatch.",
        log,
        ("head_mismatch", 10, cut.body() + log.head() + b"\n"),
    )

    log = Log()
    started(log, [READ_FILE])
    user(log, "hello")
    gap = log.add("turn_completed", {"reason": "end_turn"})
    gap["seq"] = num(gap["seq"]) + 1
    gap["event_id"] = eid(num(gap["seq"]))
    log.lines[-1] = canonical(gap)
    negative(
        root,
        "seq-gap-rejected",
        "seq jumps from 2 to 4 with a valid prev_hash: seq_mismatch.",
        log,
        ("seq_mismatch", 4),
    )

    log = base_simple()
    s = snapshot(log, None)
    child = log.fork(num(s["seq"]), CHILD, "sbx_child_01", epoch=2)
    e = child.add(
        "user_input",
        {"source": "api", "text": "hi"},
        actor="user",
        principal=ALICE,
        branch_id=BRANCH,
    )
    child.lines[-1] = canonical(e)
    negative(
        root,
        "event-branch-id-mismatch",
        "An event stored in the child segment claims the parent's branch_id. Every event's "
        "branch_id must equal its segment header's branch_id: invalid_transition.",
        child,
        ("invalid_transition", num(e["seq"])),
    )
