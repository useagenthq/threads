# pyright: strict
"""When a background result must not wake the lead (spec/schema/README.md, "Background wakes";
rule 32): after a thread or tree cancel that followed the child's spawn, after a handoff, and for
a run that ended otherwise. The late result is still recorded, with no woken. An idle cancel ends
the run cancelled (run completion)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .agents import CHILD_A, HANDOFF
from .common import ALICE, tokens
from .log import Log
from .pieces import answer, call, reduce_case, reject, result, started, user
from .policies import policy
from .run_end import run_projection
from .team_index import pending_wakes
from .teams import catalog_specs
from .wakes import KIDS, late, woken

if TYPE_CHECKING:
    import pathlib

    from .jcs import Obj

FAM = "agents_teams"


def _spawn(log: Log) -> None:
    spawn: Obj = {"agent": "scanner", "prompt": "Do your part.", "background": True}
    call(log, "spawn_agent", spawn, "call_1")
    spawned: Obj = {
        "call_id": "call_1",
        "child_thread_id": KIDS[0],
        "agent_name": "scanner",
        "mode": "background",
        "isolation": "none",
    }
    log.add("agent_spawned", spawned)
    result(log, "call_1", "scanner started in the background", origin="deferred")


def _cancelled() -> Log:
    log = Log()
    started(log, catalog_specs(("spawn_agent",)))
    user(log, "Scan in the background.")
    _spawn(log)
    answer(log, "The scan is running.")
    cancel: Obj = {"scope": "tree", "reason": "no longer needed"}
    log.add("cancel_requested", cancel, actor="host", principal=ALICE)
    return log


def _handed_off() -> Log:
    log = Log()
    started(log, [*catalog_specs(("spawn_agent",)), HANDOFF], policy=policy(handoffs=["billing"]))
    user(log, "Scan, then pass me to billing.")
    _spawn(log)
    call(log, "handoff", {"agent": "billing"}, "call_2")
    fwd = log.art(b"user: Scan, then pass me to billing.", "text/plain")
    handoff: Obj = {
        "call_id": "call_2",
        "to_agent": "billing",
        "to_thread_id": CHILD_A,
        "forwarded": "transcript",
        "forwarded_ref": fwd,
    }
    log.add("handoff", handoff)
    result(log, "call_2", "Handed off to billing.")
    log.add("turn_completed", {"reason": "handoff"})
    return log


def _failed() -> Log:
    log = Log()
    started(log, catalog_specs(("spawn_agent",)))
    user(log, "Scan in the background.")
    _spawn(log)
    log.model_request()
    log.add("turn_completed", {"reason": "error"})
    return log


def build(root: pathlib.Path) -> None:
    cases = (
        (
            "woken-after-idle-cancel-rejected",
            "Rule 32: the lead answered and waits for its child; a tree cancel follows the "
            "child's spawn, so the child's late result wakes nothing.",
            _cancelled(),
        ),
        (
            "woken-after-handoff-rejected",
            "Rule 32: the lead handed off while its background child ran; the child's late "
            "result is recorded, but a woken after the handoff is refused.",
            _handed_off(),
        ),
        (
            "woken-after-failed-run-rejected",
            "Rule 32: the run's turn ended error while its child ran; the run has ended, so the "
            "child's late result wakes nothing.",
            _failed(),
        ),
    )
    for name, desc, log in cases:
        woken(log, [late(log, "call_1", KIDS[0])])
        reject(root, (name, FAM, desc), log)
    log = _cancelled()
    late(log, "call_1", KIDS[0])
    reduce_case(
        root,
        (
            "legacy-run-cancelled-while-waiting",
            FAM,
            "The run answered and waited for its background child; a tree cancel while no turn "
            "was open ends the run cancelled, and the child's late result is recorded with no "
            "woken.",
        ),
        log,
        {"run": run_projection(log.events)},
    )
    log = _failed()
    finished: Obj = {"child_thread_id": KIDS[0], "status": "cancelled", "usage": tokens(5, 1)}
    log.add("agent_finished", finished)
    stopped: Obj = {
        "call_id": "call_1",
        "is_error": True,
        "completeness": "complete",
        "preview": "cancelled: parent run ended",
    }
    log.add("tool_result_late", stopped, actor="tool")
    reduce_case(
        root,
        (
            "legacy-run-failed-children-recorded",
            FAM,
            "The run's turn failed while its background child ran: the run is failed, the child "
            "got a tree cancel (parent run ended) in its own log, and its cancelled end is "
            "recorded in the lead's log with no woken before run() returns, leaving no "
            "pending_wakes row.",
        ),
        log,
        {"run": run_projection(log.events), "pending_wakes": pending_wakes([log])},
    )
