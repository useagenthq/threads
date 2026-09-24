# pyright: strict
"""Staged cases for semantic rules 39 (a wait's members are a set), 44 (an ask's identity) and
45 (causal bindings): each log's last event breaks the rule (spec/schema/README.md)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import ALICE, eid, text, tokens
from .pieces import answer, call, reduce_case, reject, result, user
from .team_pieces import (
    DEADLINE,
    LEAD,
    LEAD_BRANCH,
    LEAD_THREAD,
    LOG_BRANCH,
    LOG_THREAD,
    MEMBER_BRANCH,
    MEMBER_THREAD,
    RESEARCHER,
    TEAM_TOOLS,
    Route,
    at,
    body,
    config_hash,
    envelope,
    lead_log,
    provenance,
    team_log,
)
from .team_steps import FAM

if TYPE_CHECKING:
    import pathlib

    from .jcs import Obj
    from .log import Log

BOB: Obj = {"issuer": "api", "tenant": "acme", "subject": "bob"}
KID = "0192a000-0000-7000-8000-0000000000cb"


def build(root: pathlib.Path) -> None:
    for name, desc, log in _cases():
        reject(root, (name, FAM, desc), log)


def build_staged(root: pathlib.Path) -> None:
    """Staged until Phase 0 lands the pending_wakes projection it expects."""
    reduce_case(
        root,
        (
            "woken-principal-mail-run",
            FAM,
            "Rule 45's accepted twin: the woken names Alice, whose request the mail that opened "
            "the spawning turn belongs to, although the latest user_input is Bob's.",
        ),
        _woken_by_mail(ALICE),
        {"pending_wakes": []},
    )


def _asked() -> tuple[Log, str]:
    log = lead_log()
    return log, text(user(log, "Ask the researcher.")["event_id"])


def _cases() -> list[tuple[str, str, Log]]:
    out: list[tuple[str, str, Log]] = []

    log, root = _asked()
    wait: Obj = {
        "wait_id": f"{LEAD_BRANCH}:c1",
        "members": [RESEARCHER, RESEARCHER],
        "mode": "all",
        "deadline": DEADLINE,
    }
    log.add("wait_started", wait)
    out.append(("wait-members-repeat-rejected", "Rule 39: a wait lists researcher-1 twice.", log))

    log, root = _asked()
    ask = envelope(
        f"{LEAD_BRANCH}:c1",
        "ask",
        Route(LEAD, "researcher-1", provenance(root)),
        at(root),
        ask_id=f"{LEAD_BRANCH}:c9",
        deadline=DEADLINE,
        body=body("Which topic?"),
    )
    log.add("message_sent", {"envelope": ask})
    out.append(
        ("ask-id-not-its-mail-id-rejected", "Rule 44: an ask's ask_id is not its own mail_id.", log)
    )

    log, root = _asked()
    note = envelope(
        f"{MEMBER_BRANCH}:c1",
        "message",
        Route(RESEARCHER, "lead", provenance(root)),
        at(root, MEMBER_THREAD),
        body=body("Found it."),
    )
    log.add(
        "message_received",
        {"mail_id": note["mail_id"], "envelope": note},
        actor="host",
        principal=BOB,
    )
    out.append(
        (
            "receipt-actor-not-provenance-rejected",
            "Rule 45: a receipt's actor principal is not its envelope's provenance principal.",
            log,
        )
    )

    log = team_log()
    elsewhere: Obj = {
        "principal": ALICE,
        "root_request": {"thread_id": LOG_THREAD, "event_id": eid(9, LOG_BRANCH)},
        "via": [],
    }
    request: Obj = {
        "request_id": "0192d000-0000-7000-8000-000000000001",
        "op": "send",
        "principal": ALICE,
        "body_hash": "0" * 64,
        "provenance": elsewhere,
    }
    log.add("operator_request", request, actor="host", principal=ALICE)
    out.append(
        (
            "operator-request-root-not-itself-rejected",
            "Rule 45: an operator_request's provenance names another event as its root request.",
            log,
        )
    )

    log, root = _asked()
    parent: Obj = {
        "thread_id": LEAD_THREAD,
        "branch_id": LEAD_BRANCH,
        "event_id": root,
        "relation": "team_member",
    }
    started: Obj = {
        "member": RESEARCHER,
        "agent": "researcher",
        "config_hash": config_hash("researcher"),
        "thread_id": MEMBER_THREAD,
        "parent": parent,
        "provenance": provenance(root),
    }
    log.add("member_started", started)
    out.append(
        (
            "member-started-parent-not-itself-rejected",
            "Rule 45: a lead's member_started names another event as the member's parent.",
            log,
        )
    )
    out.append(
        (
            "woken-principal-mail-run-rejected",
            "Rule 45: after Bob's run, a researcher's message of Alice's operator request opens "
            "the lead's turn, which starts a background child; its woken names Bob (the latest "
            "user_input), not Alice, whose request opened the spawning turn. "
            "woken-principal-mail-run is the same log with Alice.",
            _woken_by_mail(BOB),
        )
    )
    return out


def _woken_by_mail(principal: Obj) -> Log:
    """Bob's run comes first. Then mail of Alice's operator request (a researcher she started)
    opens the lead's next turn, which starts a background child. Its late result's woken names
    `principal`: Alice is right, and Bob (the latest user_input) is what a naive reader picks."""
    log = lead_log(("spawn_agent", *TEAM_TOOLS))
    bob: Obj = {"source": "api", "text": "What is on the list today?"}
    log.add("user_input", bob, actor="user", principal=BOB)
    answer(log, "Nothing yet.")
    request = eid(2, LOG_BRANCH)
    alice = provenance(request, thread=LOG_THREAD)
    note = envelope(
        f"{MEMBER_BRANCH}:c1",
        "message",
        Route(RESEARCHER, "lead", alice),
        at(request, LOG_THREAD),
        body=body("Scan the dependencies."),
    )
    log.add(
        "message_received",
        {"mail_id": note["mail_id"], "envelope": note},
        actor="host",
        principal=ALICE,
    )
    spawn: Obj = {"agent": "scanner", "prompt": "Do your part.", "background": True}
    call(log, "spawn_agent", spawn, "call_1")
    spawned: Obj = {
        "call_id": "call_1",
        "child_thread_id": KID,
        "agent_name": "scanner",
        "mode": "background",
        "isolation": "none",
    }
    log.add("agent_spawned", spawned)
    result(log, "call_1", "scanner started in the background", origin="deferred")
    answer(log, "The scan is running.")
    finished: Obj = {"child_thread_id": KID, "status": "completed", "usage": tokens(40, 8)}
    log.add("agent_finished", finished)
    late: Obj = {
        "call_id": "call_1",
        "is_error": False,
        "completeness": "complete",
        "preview": "Clean.",
    }
    cause = log.add("tool_result_late", late)
    log.add("woken", {"causes": [cause["event_id"]]}, actor="host", principal=principal)
    return log
