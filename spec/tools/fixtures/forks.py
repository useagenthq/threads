# pyright: strict
"""Fork and child-branch cases."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import ALICE, CHILD, DAY, NOW, T0, num, obj, sha, tokens
from .jcs import Obj, canonical
from .log import reduce
from .pieces import SNAPSHOT_SCRIPT, base_simple, case, negative, snapshot, user, write_case

if TYPE_CHECKING:
    import pathlib


SANDBOX: Obj = {"snapshots": {"snap_01": SNAPSHOT_SCRIPT}}


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
        extra={"sandbox.json": SANDBOX},
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
        extra={"sandbox.json": SANDBOX},
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
        extra={"sandbox.json": SANDBOX},
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

    _knowledge(root)
    _lost_restore(root)
    _crash_mid_fork(root)
    _repair(root)
    _duplicate_id(root)


def _knowledge(root: pathlib.Path) -> None:
    for policy, revision in (("pinned", 7), ("current", None)):
        log = base_simple()
        s = snapshot(log, None, knowledge_revision=7)
        child = log.fork(num(s["seq"]), CHILD, "sbx_child_01", epoch=2, knowledge=policy)
        inp: Obj = {"fork_at_event_id": s["event_id"], "new_branch_id": CHILD}
        if policy == "current":
            inp["knowledge_policy"] = "current"
        write_case(
            root,
            case(
                f"fork-knowledge-{policy}",
                "knowledge",
                "fork",
                "The fork records its corpus policy. pinned is the default: "
                "the child searches as of the snapshot's knowledge_revision (7). current: the "
                "child searches the live corpus. Either way, recorded retrievals replay as "
                "recorded and are never re-run.",
                sandbox_script="sandbox.json",
                input=inp,
            ),
            log,
            {
                "outcome": "ok",
                "appended": [
                    {
                        "type": "fork",
                        "branch_id": CHILD,
                        "data": child.events[-1]["data"],
                    }
                ],
                "fork": {
                    "child_created": True,
                    "at_seq": s["seq"],
                    "parent_unchanged": True,
                    "knowledge_revision": revision,
                },
            },
            extra={"sandbox.json": SANDBOX},
        )


def _lost_restore(root: pathlib.Path) -> None:
    for lookup in ("found", "unsupported"):
        log = base_simple()
        s = snapshot(log, None)
        child = log.fork(num(s["seq"]), CHILD, "sbx_child_01", epoch=2)
        script: Obj = {
            "snapshots": {
                "snap_01": {
                    **SNAPSHOT_SCRIPT,
                    "restore_response": "lost",
                    "create_lookup": lookup,
                }
            }
        }
        inp: Obj = {"fork_at_event_id": s["event_id"], "new_branch_id": CHILD}
        expected: Obj
        if lookup == "found":
            desc = (
                "The provider creates the child sandbox but the response is lost. The ledger row "
                "was written with its operation_key before the call, and the adapter finds the "
                "sandbox by that key: it is verified and used. Nothing is created twice."
            )
            expected = {
                "outcome": "ok",
                "appended": [
                    {
                        "type": "fork",
                        "branch_id": CHILD,
                        "data": child.events[-1]["data"],
                    }
                ],
                "fork": {
                    "child_created": True,
                    "at_seq": s["seq"],
                    "parent_unchanged": True,
                },
                "resources": {
                    "creates": 1,
                    "rows": [{"kind": "sandbox", "state": "live"}],
                },
            }
        else:
            desc = (
                "The child sandbox's create response is lost and the adapter can neither create "
                "idempotently by operation_key nor look the key up. The fork fails with "
                "resource_unknown; the ledger row stays unknown and parks for an operator. "
                "Creation is never retried blindly and no child is listed."
            )
            expected = {
                "outcome": "error",
                "error": {"code": "resource_unknown", "seq": s["seq"]},
                "appended": [],
                "fork": {"child_created": False, "parent_unchanged": True},
                "resources": {
                    "creates": 1,
                    "rows": [{"kind": "sandbox", "state": "unknown"}],
                },
            }
        write_case(
            root,
            case(
                f"fork-restore-lost-{lookup}",
                "sandboxes",
                "fork",
                desc,
                sandbox_script="sandbox.json",
                input=inp,
            ),
            log,
            expected,
            extra={"sandbox.json": script},
        )


def _crash_mid_fork(root: pathlib.Path) -> None:
    log = base_simple()
    s = snapshot(log, None)
    write_case(
        root,
        case(
            "fork-crash-no-orphan",
            "sandboxes",
            "fork",
            "The host dies after the child sandbox is restored and before the child's fork "
            "event is appended (sandbox.json restore_response: crash). A fork is never resumed: "
            "on restart, recovery marks the child fork_failed and releases every ledger row "
            "the fork wrote. No child is listed and the parent is unchanged.",
            sandbox_script="sandbox.json",
            input={"fork_at_event_id": s["event_id"], "new_branch_id": CHILD},
        ),
        log,
        {
            "outcome": "ok",
            "appended": [],
            "fork": {
                "child_created": False,
                "parent_unchanged": True,
                "child_state": "fork_failed",
            },
            "resources": {"creates": 1, "rows": [{"kind": "sandbox", "state": "released"}]},
        },
        extra={
            "sandbox.json": {
                "snapshots": {"snap_01": {**SNAPSHOT_SCRIPT, "restore_response": "crash"}}
            }
        },
    )


def _repair(root: pathlib.Path) -> None:
    log = base_simple()
    child = log.fork(log.seq, CHILD, None, epoch=2)
    write_case(
        root,
        case(
            "repair-child-inspection-only",
            "log_fork_test",
            "recover",
            "An operator repair fork (fork{reason: repair}) has no sandbox that matches its "
            "log, so it is inspection-only: it reduces and exports, but acquiring it to run "
            "fails with branch_not_runnable and nothing is appended. To continue work, fork it "
            "at an eligible snapshot in its resolved chain.",
            model_script="model.json",
        ),
        child,
        {
            "outcome": "error",
            "error": {"code": "branch_not_runnable", "seq": child.seq},
            "state": reduce(child, NOW),
            "appended": [],
        },
        extra={"model.json": {"responses": []}},
    )


def _duplicate_id(root: pathlib.Path) -> None:
    log = base_simple()
    s = snapshot(log, None)
    child = log.fork(num(s["seq"]), CHILD, "sbx_child_01", epoch=2)
    e = child.add("user_input", {"source": "api", "text": "Again."}, actor="user", principal=ALICE)
    e["event_id"] = log.events[0]["event_id"]
    child.lines[-1] = canonical(e)
    negative(
        root,
        "event-id-duplicate-across-fork-rejected",
        "A child event reuses the event_id of an ancestor event. Each physical branch is "
        "unique on its own, but ids must be unique along the resolved chain so an id-only "
        "reference is never ambiguous: invalid_transition.",
        child,
        ("invalid_transition", child.seq),
    )
