# pyright: strict
"""Structural import errors: segment layout and the per-line-before-chain check order."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import ALICE, BRANCH, CHILD, num, obj, sha
from .jcs import Obj, canonical
from .pieces import base_simple, negative, snapshot

if TYPE_CHECKING:
    import pathlib


def build(root: pathlib.Path) -> None:
    log = base_simple()
    negative(
        root,
        "event-before-header-rejected",
        "The export starts with an event line; the branch header is missing. Every line is valid "
        "on its own, and a segment must start with its header: invalid_transition at the first "
        "line (invalid_line is only for a line that fails on its own).",
        log,
        (
            "invalid_transition",
            1,
            b"".join(ln + b"\n" for ln in log.lines[1:]) + log.head() + b"\n",
        ),
    )

    log = base_simple()
    s = snapshot(log, None)
    child = log.fork(num(s["seq"]), CHILD, "sbx_child_01", epoch=2)
    child.lines.pop()
    child.events.pop()
    e = child.add("user_input", {"source": "api", "text": "hi"}, actor="user", principal=ALICE)
    negative(
        root,
        "child-header-without-fork-rejected",
        "A child segment header is followed by a user_input instead of its fork event. A child "
        "header must be immediately followed by the fork that links it: invalid_transition.",
        child,
        ("invalid_transition", num(e["seq"])),
    )

    log = base_simple()
    fork = log.add(
        "fork",
        {
            "parent_branch_id": BRANCH,
            "at_hash": sha(log.lines[-1]),
            "reason": "snapshot",
            "sandbox_id": "sbx_child_01",
            "knowledge_policy": "pinned",
        },
    )
    negative(
        root,
        "fork-event-mid-segment-rejected",
        "A schema-valid fork event in the middle of a segment. fork is only valid as the first "
        "event after a child header: invalid_transition.",
        log,
        ("invalid_transition", num(fork["seq"])),
    )

    log = base_simple()
    bad: Obj = dict(log.events[2])
    bad["data"] = {**obj(bad["data"]), "unexpected": True}
    bad["prev_hash"] = sha(b"not the previous line")
    log.lines[3] = canonical(bad)
    negative(
        root,
        "line-schema-error-before-chain",
        "Line seq 3 has an extra data field and a wrong prev_hash. Each line's own checks "
        "(schema included) run before chain checks, so the result is invalid_line, not "
        "prev_hash_mismatch.",
        log,
        ("invalid_line", 3),
    )
