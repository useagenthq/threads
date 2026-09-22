# pyright: strict
"""Fork and child-branch cases."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import ALICE, CHILD, DAY, NOW, T0, num, obj, sha, tokens
from .jcs import Obj, canonical
from .log import reduce
from .pieces import base_simple, case, negative, snapshot, user, write_case

if TYPE_CHECKING:
    import pathlib


def build(root: pathlib.Path) -> None:
    # fork-at-snapshot-ok
    log = base_simple()
    s = snapshot(log, T0 + DAY)
    child = log.fork(num(s["seq"]), CHILD, "sbx_child_01", epoch=2)
    write_case(
        root,
        case(
            "fork-at-snapshot-ok",
            "log_fork_test",
            "fork",
            "Fork at a quiescent snapshot. The child gets its own header and a fork event at "
            "seq at_seq+1 that references the parent (parent_branch_id, at_hash of the parent's "
            "snapshot line); parent rows are referenced, never copied or rewritten. The "
            "snapshot restores into an isolated sandbox. The parent is unchanged.",
            sandbox_script="sandbox.json",
            input={"fork_at_event_id": s["event_id"], "new_branch_id": CHILD},
        ),
        log,
        {
            "outcome": "ok",
            "state": reduce(log, NOW),
            "appended": [
                {
                    "type": "fork",
                    "seq": num(s["seq"]) + 1,
                    "branch_id": CHILD,
                    "epoch": 2,
                    "data": child.events[-1]["data"],
                }
            ],
            "fork": {
                "child_created": True,
                "at_seq": s["seq"],
                "parent_unchanged": True,
            },
        },
        extra={"sandbox.json": {"snapshots": {"snap_01": {"restore_sandbox_id": "sbx_child_01"}}}},
    )

    # reduce-child-branch: a child export reduces over its resolved chain
    child.add(
        "user_input",
        {"source": "api", "text": "Now in the child."},
        actor="user",
        principal=ALICE,
    )
    write_case(
        root,
        case(
            "reduce-child-branch",
            "log_fork_test",
            "reduce",
            "A child branch export: the root header and the parent's lines through at_seq "
            "(referenced rows, byte-identical), then the child's own header, its fork event and "
            "one more event. Seq is contiguous across segments, each segment chains from its "
            "own header, and the fork's at_hash binds to the parent's snapshot line. State "
            "comes from the resolved chain; branch_id is the child.",
        ),
        child,
        {"outcome": "ok", "state": reduce(child, NOW)},
    )

    # fork-not-at-snapshot-error
    log = base_simple()
    snapshot(log, None)
    user(log, "Now read LICENSE.")
    r = log.model_request()
    m = log.model_response(
        r,
        [
            {
                "type": "tool_use",
                "call_id": "call_2",
                "name": "read_file",
                "input": {"path": "LICENSE"},
            }
        ],
        "tool_use",
        tokens(180, 16),
    )
    log.tool_call(r, "call_2", "read_file", {"path": "LICENSE"})
    write_case(
        root,
        case(
            "fork-not-at-snapshot-error",
            "log_fork_test",
            "fork",
            "Fork requested at a model_response inside an open turn with a pending call. Must "
            "fail with no_snapshot_boundary, create no child and restore no sandbox.",
            sandbox_script="sandbox.json",
            input={"fork_at_event_id": m["event_id"], "new_branch_id": CHILD},
        ),
        log,
        {
            "outcome": "error",
            "error": {"code": "no_snapshot_boundary", "seq": m["seq"]},
            "state": reduce(log, NOW),
            "appended": [],
            "fork": {"child_created": False, "parent_unchanged": True},
        },
        extra={"sandbox.json": {"snapshots": {"snap_01": {"restore_sandbox_id": "sbx_child_01"}}}},
    )

    # fork-snapshot-expired
    log = base_simple()
    s = snapshot(log, NOW - 1)
    write_case(
        root,
        case(
            "fork-snapshot-expired",
            "log_fork_test",
            "fork",
            "Fork at a snapshot whose expires_at has passed on the injected clock. It is not a "
            "fork point; the fork fails with snapshot_expired, creates no child and leaves "
            "nothing to clean up.",
            sandbox_script="sandbox.json",
            input={"fork_at_event_id": s["event_id"], "new_branch_id": CHILD},
        ),
        log,
        {
            "outcome": "error",
            "error": {"code": "snapshot_expired", "seq": s["seq"]},
            "state": reduce(log, NOW),
            "appended": [],
            "fork": {"child_created": False, "parent_unchanged": True},
        },
        extra={"sandbox.json": {"snapshots": {"snap_01": {"restore_sandbox_id": "sbx_child_01"}}}},
    )

    # fork-at-hash-mismatch: a child export whose fork link does not match the parent line
    log = base_simple()
    s = snapshot(log, None)
    child = log.fork(num(s["seq"]), CHILD, "sbx_child_01", epoch=2)
    bad: Obj = dict(child.events[-1])
    bad["data"] = {**obj(bad["data"]), "at_hash": sha(b"not the parent line")}
    child.lines[-1] = canonical(bad)
    negative(
        root,
        "fork-at-hash-mismatch",
        "A child export whose fork event's at_hash does not equal the hash of the parent's line "
        "at at_seq. The child is not bound to that parent chain: prev_hash_mismatch at the fork "
        "seq.",
        child,
        ("prev_hash_mismatch", num(s["seq"]) + 1),
    )
