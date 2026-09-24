# pyright: strict
"""Background wakes of legacy subagents (spec/schema/README.md, "Background wakes"; rules 32 and
45): readable by both runtimes now, so these cases are in the corpus. A late result and its
woken are one append; wakes are recorded by run; a late result during an open turn is ordinary
input to that turn."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import ALICE, text, tokens
from .log import Log
from .pieces import answer, call, reduce_case, reject, result, started, user
from .projections import children
from .teams import catalog_specs

if TYPE_CHECKING:
    import pathlib

    from .jcs import Obj

FAM = "agents_teams"
BOB: Obj = {"issuer": "api", "tenant": "acme", "subject": "bob"}
KIDS = ("0192a000-0000-7000-8000-0000000000c9", "0192a000-0000-7000-8000-0000000000ca")
TOOLS = ("spawn_agent",)


def build(root: pathlib.Path) -> None:
    _one_append(root)
    _by_run(root)
    _ordinary_input(root)
    _after_pre_woken(root)
    for name, desc, log in _rejected():
        reject(root, (name, FAM, desc), log)


def _log() -> Log:
    log = Log()
    started(log, catalog_specs(TOOLS))
    return log


def _input(log: Log, t: str, principal: Obj = ALICE) -> Obj:
    return log.add("user_input", {"source": "api", "text": t}, actor="user", principal=principal)


def spawn_background(log: Log, cid: str, kid: str) -> None:
    spawn: Obj = {"agent": "scanner", "prompt": "Do your part.", "background": True}
    call(log, "spawn_agent", spawn, cid)
    spawned: Obj = {
        "call_id": cid,
        "child_thread_id": kid,
        "agent_name": "scanner",
        "mode": "background",
        "isolation": "none",
    }
    log.add("agent_spawned", spawned)
    result(log, cid, "scanner started in the background", origin="deferred")


def late(log: Log, cid: str, kid: str) -> Obj:
    """A background child's end: its agent_finished and its call's late result."""
    finished: Obj = {"child_thread_id": kid, "status": "completed", "usage": tokens(40, 8)}
    log.add("agent_finished", finished)
    late: Obj = {"call_id": cid, "is_error": False, "completeness": "complete", "preview": "Done."}
    return log.add("tool_result_late", late)


def woken(log: Log, causes: list[Obj], principal: Obj = ALICE) -> Obj:
    ids = [c["event_id"] for c in causes]
    return log.add("woken", {"causes": ids}, actor="host", principal=principal)


def _two_runs(second: Obj) -> Log:
    """Alice's run starts one background child; a second run (`second`'s) starts another."""
    log = _log()
    _input(log, "Scan the dependencies in the background.")
    spawn_background(log, "call_1", KIDS[0])
    answer(log, "The first scan is running.")
    _input(log, "Scan the licenses in the background.", second)
    spawn_background(log, "call_2", KIDS[1])
    answer(log, "The second scan is running.")
    return log


def one_append_log() -> Log:
    """Two background children; the first ends, recorded with its woken in one append."""
    log = _log()
    user(log, "Scan the dependencies and the licenses in the background.")
    spawn_background(log, "call_1", KIDS[0])
    spawn_background(log, "call_2", KIDS[1])
    answer(log, "Both scans are running.")
    woken(log, [late(log, "call_1", KIDS[0])])
    return log


def _one_append(root: pathlib.Path) -> None:
    log = one_append_log()
    reduce_case(
        root,
        (
            "legacy-wake-in-the-late-result-append",
            FAM,
            "Two background children run; the first ends while the lead has no open turn and no "
            "cancel barrier. One append records its agent_finished, its tool_result_late and a "
            "woken naming it, and woken opens the lead's next turn (in_turn). "
            "subagent-background-notify is a log written before woken existed: it stays idle, "
            "never woken retroactively.",
        ),
        log,
        {"children": children(log)},
    )


def _by_run(root: pathlib.Path) -> None:
    log = _two_runs(BOB)
    woken(log, [late(log, "call_1", KIDS[0])])
    answer(log, "The dependency scan is clean.")
    woken(log, [late(log, "call_2", KIDS[1])], BOB)
    reduce_case(
        root,
        (
            "legacy-wakes-grouped-by-run",
            FAM,
            "Two background children of two runs (Alice's, then Bob's) end at one boundary. The "
            "writer records them by run, in spawn order: Alice's child's end and a woken with her "
            "principal open one turn; only after it ends are Bob's child's end and his woken "
            "recorded, opening the next turn.",
        ),
        log,
        {"children": children(log)},
    )


def _ordinary_input(root: pathlib.Path) -> None:
    log = _log()
    _input(log, "Scan the dependencies in the background.")
    spawn_background(log, "call_1", KIDS[0])
    answer(log, "The scan is running.")
    _input(log, "What else is new?", BOB)
    late(log, "call_1", KIDS[0])
    answer(log, "The scan you started earlier is clean.")
    reduce_case(
        root,
        (
            "legacy-late-result-in-another-runs-turn",
            FAM,
            "A child of Alice's run reports while Bob's run has a turn open: its agent_finished "
            "and tool_result_late are ordinary input to that turn, with no woken (Gate 1 §2.7.3), "
            "as in every log written before woken existed.",
        ),
        log,
        {"children": children(log)},
    )


def _after_pre_woken(root: pathlib.Path) -> None:
    log = _log()
    user(log, "Scan the dependencies and the licenses in the background.")
    spawn_background(log, "call_1", KIDS[0])
    spawn_background(log, "call_2", KIDS[1])
    answer(log, "Both scans are running.")
    late(log, "call_1", KIDS[0])  # written before woken existed: no wake
    woken(log, [late(log, "call_2", KIDS[1])])
    reduce_case(
        root,
        (
            "legacy-wake-after-pre-woken-late-result",
            FAM,
            "The upgrade: a writer from before woken recorded the first child's late result with "
            "no wake; the upgraded writer then records the second child's end and a woken naming "
            "only it. The causes are the tail of the late-result block, so the earlier result is "
            "never woken retroactively and the new wake is accepted.",
        ),
        log,
        {"children": children(log)},
    )


def _rejected() -> list[tuple[str, str, Log]]:
    out: list[tuple[str, str, Log]] = []
    log = _two_runs(ALICE)
    woken(log, [late(log, "call_1", KIDS[0]), late(log, "call_2", KIDS[1])])
    out.append(
        (
            "woken-mixed-runs-rejected",
            "Rule 32: one woken names late results of two runs with the same principal (Alice's "
            "first and second requests): a wake turn has one root request.",
            log,
        )
    )
    log = _log()
    user(log, "Scan in the background.")
    spawn_background(log, "call_1", KIDS[0])
    answer(log, "Running.")
    cause = late(log, "call_1", KIDS[0])
    log.add(
        "woken", {"causes": [cause["event_id"], cause["event_id"]]}, actor="host", principal=ALICE
    )
    out.append(("woken-cause-repeated-rejected", "Rule 32: woken names one cause twice.", log))
    log = _log()
    user(log, "Scan in the background.")
    spawn_background(log, "call_1", KIDS[0])
    woken(log, [late(log, "call_1", KIDS[0])])
    out.append(("woken-with-open-turn-rejected", "Rule 32: woken while a turn is open.", log))
    log = _log()
    first = user(log, "Scan in the background.")
    answer(log, "Started.")
    log.add("woken", {"causes": [text(first["event_id"])]}, actor="host", principal=ALICE)
    out.append(
        (
            "woken-cause-not-late-result-rejected",
            "Rule 32: woken names a cause that is not a background child's late result.",
            log,
        )
    )
    log = _log()
    user(log, "Scan in the background.")
    spawn_background(log, "call_1", KIDS[0])
    answer(log, "Running.")
    woken(log, [late(log, "call_1", KIDS[0])], BOB)
    out.append(
        (
            "woken-principal-not-spawning-run-rejected",
            "Rule 45: a woken acts for Bob, not Alice, whose run spawned the child.",
            log,
        )
    )
    return out
