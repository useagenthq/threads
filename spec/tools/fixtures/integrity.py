# pyright: strict
"""Log integrity cases: reduce, torn tail, unknown events, chain and head."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import ALICE, BRANCH, CHILD, NOW, aref, eid, num
from .jcs import JsonValue, canonical
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


# (name suffix, canonical bytes, replacement). Each turns the last line into a non-canonical
# spelling of the same value; admission compares a line with its RFC 8785 form before the schema.
NONCANONICAL = (
    ("exponent", b'"n":1000', b'"n":1e3'),
    ("fraction", b'"n":1000', b'"n":1000.0'),
    ("negative-zero", b'"z":0', b'"z":-0'),
    ("key-order", b'"n":1000,"s":"a/b\\u001fA","z":0', b'"z":0,"s":"a/b\\u001fA","n":1000'),
    ("whitespace", b'"n":1000', b'"n": 1000'),
    ("escape-nonminimal", b'fA"', b'f\\u0041"'),
    ("escape-uppercase", b"\\u001f", b"\\u001F"),
    ("escape-slash", b"a/b", b"a\\/b"),
)


def _noncanonical(root: pathlib.Path) -> None:
    for suffix, good, bad in NONCANONICAL:
        log = Log()
        started(log, [READ_FILE])
        read_turn(log)
        log.add("telemetry_ping", {"n": 1000, "s": "a/b\u001fA", "z": 0}, critical=False)
        if good not in log.lines[-1]:
            raise AssertionError(f"{suffix}: {good!r} not in the canonical line")
        log.lines[-1] = log.lines[-1].replace(good, bad)
        negative(
            root,
            f"line-noncanonical-{suffix}",
            f"The last line holds the same JSON value in a non-canonical spelling ({suffix}). A "
            "reader admits a line only if its bytes equal the RFC 8785 form of its parsed value: "
            "invalid_line, before the schema and before the chain. The chain and head hash the "
            "stored bytes, so they still verify.",
            log,
            ("invalid_line", log.seq),
        )


MAX_DEPTH = 64


def _too_deep(root: pathlib.Path) -> None:
    # The line object is depth 1 and its data object depth 2, so this puts arrays at 3..65.
    deep: JsonValue = []
    for _ in range(MAX_DEPTH - 2):
        deep = [deep]
    log = Log()
    started(log, [READ_FILE])
    read_turn(log)
    log.add("telemetry_ping", {"deep": deep}, critical=False)
    negative(
        root,
        "line-nesting-too-deep",
        f"The last line nests arrays and objects {MAX_DEPTH + 1} levels deep (the line itself is "
        f"level 1). Readers admit at most {MAX_DEPTH} levels, so parsing never depends on stack "
        "depth: invalid_line, even though the line is otherwise canonical and ignorable.",
        log,
        ("invalid_line", log.seq),
    )


def _admission(root: pathlib.Path) -> None:
    _noncanonical(root)
    _too_deep(root)
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
    log = Log()
    log.lines[0] = log.lines[0].replace(b'"format":"threads.log"', b'"format":["threads.log"]')
    started(log, [READ_FILE])
    read_turn(log)
    negative(
        root,
        "header-format-array",
        "The header's format is a JSON array, not a string: invalid_line. A reader must not "
        "crash on a non-string format.",
        log,
        ("invalid_line", 0),
    )


def _torn(root: pathlib.Path, name: str, desc: str, served: Log, dropped: bytes) -> None:
    """An export of `served` plus an unterminated final chunk: import drops only that chunk."""
    good = served.body()
    write_case(
        root,
        case(name, "log", "recover", desc),
        served,
        {
            "outcome": "ok",
            "state": reduce(served, NOW),
            "committed_bytes": len(good),
            "head_verified": False,
            "appended": [
                {
                    "type": "log_repaired",
                    "critical": False,
                    "epoch": 2,
                    "data": {
                        "truncated_bytes": len(dropped),
                        "at_offset": len(good),
                        "dropped_ref": aref(dropped, "application/octet-stream"),
                    },
                }
            ],
        },
        extra={"log.jsonl": good + dropped},
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
        {
            "outcome": "ok",
            "state": reduce(log, NOW),
            "committed_bytes": len(log.body()),
            "head_verified": True,
        },
    )

    # torn tails (JSONL import only; the SQLite store cannot tear). "\n" is the commit marker.
    log = base_simple()
    nxt = canonical(user(log, "A second question that never finished writing."))
    log.lines.pop()
    log.events.pop()
    _torn(
        root,
        "torn-tail-truncated",
        "An export whose last line is a partial write with no newline and no head "
        "checkpoint. Import serves the valid prefix, never edits the source file, flags the "
        "head as unverified, keeps the dropped bytes as an artifact, and the imported "
        "branch records log_repaired (critical: false). Nothing else is appended because "
        "the prefix is balanced.",
        log,
        nxt[:57],
    )
    log = base_simple()
    _torn(
        root,
        "torn-tail-head-unterminated",
        "A healthy export with only its final newline missing. Newline is the commit marker, "
        "so the unterminated head is a torn tail even though it parses and verifies: served "
        "prefix without it, head unverified, head bytes kept as the dropped artifact.",
        log,
        log.head(),
    )
    log = base_simple()
    cut = log.copy()
    cut.lines, cut.events = log.lines[:-1], log.events[:-1]
    _torn(
        root,
        "torn-tail-wrong-head-unterminated",
        "The last event was removed and the head (seq 10) has no final newline. An "
        "unterminated final chunk is torn whatever it says, so this is a torn tail, not "
        "head_mismatch (compare head-checkpoint-suffix-removed, where the head is terminated).",
        cut,
        log.head(),
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
