# pyright: strict
"""Staged Phase 0 cases for run completion with legacy background children (spec/schema/README.md,
"Run completion"): the run waits for the children it spawned, and a child that parks parks the
run. Phase 0 (the legacy wake) moves this family into the corpus; the team half is run_cases."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .log import Log
from .pieces import answer, call, reduce_case, started, user
from .run_end import run_projection
from .team_steps import FAM
from .teams import catalog_specs
from .wakes import KIDS, late, one_append_log, woken

if TYPE_CHECKING:
    import pathlib

    from .jcs import Obj


def build(root: pathlib.Path) -> None:
    for name, desc, log in _cases():
        reduce_case(root, (name, FAM, desc), log, {"run": run_projection(log.events)})


def _cases() -> list[tuple[str, str, Log]]:
    out: list[tuple[str, str, Log]] = []
    log = one_append_log()
    answer(log, "The dependency scan is clean.")
    out.append(
        (
            "legacy-run-waits-for-its-children",
            "A run with two background children: the first reported and woke the lead, which "
            "answered; the second still runs, so the run is still running.",
            log.copy(),
        )
    )
    woken(log, [late(log, "call_2", KIDS[1])])
    answer(log, "Both scans are clean.")
    out.append(
        (
            "legacy-run-final-answer-after-wake",
            "Both background children reported and each woke the lead: the run is completed with "
            "the last wake turn's answer.",
            log,
        )
    )
    out.append(_child_parks())
    return out


def _child_parks() -> tuple[str, str, Log]:
    log = Log()
    started(log, catalog_specs(("spawn_agent",)))
    user(log, "Review the diff.")
    call(log, "spawn_agent", {"agent": "reviewer", "prompt": "Review it."}, "call_1")
    spawned: Obj = {
        "call_id": "call_1",
        "child_thread_id": KIDS[0],
        "agent_name": "reviewer",
        "mode": "foreground",
        "isolation": "none",
    }
    log.add("agent_spawned", spawned)
    child: Obj = {"kind": "child", "id": KIDS[0]}
    log.add("parked", {"address": child, "reason": "awaiting_approval"})
    return (
        "legacy-run-child-parks",
        "A foreground child parks, which parks the lead mid-turn (the spawn call stays "
        "pending): the run returns parked, not running.",
        log,
    )
