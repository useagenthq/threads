# pyright: strict
"""Shared team fixture pieces: ids, member refs, provenance and mail envelopes
(spec/schema/README.md, "Teams")."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .common import ADAPTER, ALICE, MODEL, PARAMS, T0, eid, obj, sha, text
from .jcs import canonical
from .log import Log
from .teams import catalog_specs

if TYPE_CHECKING:
    from .jcs import JsonValue, Obj

TEAM = "0192c000-0000-7000-8000-000000000001"
LEAD_THREAD = "0192a000-0000-7000-8000-0000000000b1"
LEAD_BRANCH = "0192b000-0000-7000-8000-0000000000b1"
MEMBER_THREAD = "0192a000-0000-7000-8000-0000000000b2"
MEMBER_BRANCH = "0192b000-0000-7000-8000-0000000000b2"
WRITER_THREAD = "0192a000-0000-7000-8000-0000000000b4"
WRITER_BRANCH = "0192b000-0000-7000-8000-0000000000b4"
LOG_THREAD = "0192a000-0000-7000-8000-0000000000b3"
LOG_BRANCH = "0192b000-0000-7000-8000-0000000000b3"
REQUEST = "0192d000-0000-7000-8000-000000000001"
TENANT = "acme"
DEADLINE = T0 + 120_000


def ref(name: str, generation: int = 1) -> Obj:
    return {"tenant": TENANT, "team": TEAM, "name": name, "generation": generation}


LEAD = ref("lead")
RESEARCHER = ref("researcher-1")
WRITER = ref("writer-1")


def provenance(root_event: str, principal: Obj = ALICE, thread: str = LEAD_THREAD) -> Obj:
    return {
        "principal": principal,
        "root_request": {"thread_id": thread, "event_id": root_event},
        "via": [],
    }


@dataclass(frozen=True, slots=True)
class Route:
    """Who sends a mail, to which member name (or the team log), under which provenance."""

    sender: Obj
    to: str
    prov: Obj


def at(event_id: str, thread: str = LEAD_THREAD) -> Obj:
    """A RequestRef: the event that caused a mail."""
    return {"thread_id": thread, "event_id": event_id}


def envelope(mail_id: str, kind: str, route: Route, causal: Obj, **per_kind: JsonValue) -> Obj:
    """A mail envelope; per_kind adds ask_id, monitor_id, deadline, reason, code, result, body."""
    to: JsonValue = "team_log" if route.to == "team_log" else {"name": route.to, "generation": 1}
    return {
        "mail_id": mail_id,
        "kind": kind,
        "team": TEAM,
        "from": route.sender,
        "to": to,
        "provenance": route.prov,
        "causal": causal,
        **per_kind,
    }


def completed(member: Obj, answer: str) -> Obj:
    return {"member": member, "status": "completed", "output": {"text": answer}}


def failed(member: Obj, code: str) -> Obj:
    return {
        "member": member,
        "status": "failed",
        "error": {"code": code, "message": f"rebind failed: {code}"},
    }


def body(t: str) -> Obj:
    return {"text": t}


# ---------- the three logs of one team ----------
TEAM_TOOLS = ("ask", "cancel", "monitor", "reply", "send", "start", "wait")


def _pin(log: Log, agent: str, extra: Obj, tools: tuple[str, ...] = TEAM_TOOLS) -> Obj:
    cfg: Obj = {
        "agent_name": agent,
        "instructions": f"You are the {agent}.",
        "model": MODEL,
        "model_params": PARAMS,
        "adapter": ADAPTER,
        "tools": catalog_specs(tools),
    }
    return log.add("thread_started", {**cfg, "config_hash": sha(canonical(cfg)), **extra})


def lead_log(tools: tuple[str, ...] = TEAM_TOOLS) -> Log:
    """The lead's log: thread_started carries the team, whose log opens in the same append."""
    log = Log(LEAD_BRANCH, thread=LEAD_THREAD)
    _pin(log, "lead", {"team": {"id": TEAM, "log_branch_id": LOG_BRANCH}}, tools)
    return log


def member_log(
    parent_event: str,
    agent: str = "researcher",
    where: tuple[str, str] = (MEMBER_BRANCH, MEMBER_THREAD),
) -> Log:
    """A member's log, opened at materialize: its parent is the lead's member_started (or the
    lead's thread_started for an operator start). where is its (branch, thread)."""
    log = Log(where[0], thread=where[1])
    parent: Obj = {
        "thread_id": LEAD_THREAD,
        "branch_id": LEAD_BRANCH,
        "event_id": parent_event,
        "relation": "team_member",
    }
    _pin(log, agent, {"parent": parent})
    return log


def team_log() -> Log:
    log = Log(LOG_BRANCH, thread=LOG_THREAD)
    log.add("team_opened", {"team": TEAM, "lead": LEAD, "lead_thread_id": LEAD_THREAD})
    return log


def config_hash(agent: str) -> str:
    """The config_hash member_log pins for agent."""
    return text(obj(member_log(eid(1), agent).events[0]["data"])["config_hash"])
