# pyright: strict
"""Teams Phase 2 fixture pieces (spec/schema/README.md, "Teams Phase 2"): the tenant's host team
at its derived ids, host member logs, a caller's log and the mail between them. The example is
lane 29's: support (a caller) asks billing (a host member)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import ADAPTER, ALICE, MODEL, PARAMS, T0, eid, obj, sha, text
from .host_ids import derived as derive
from .jcs import canonical
from .log import Log
from .pieces import call
from .team_pieces import body
from .team_steps import host, received
from .teams import catalog_specs

if TYPE_CHECKING:
    from .jcs import JsonValue, Obj

TENANT = "acme"


def derived(part: str, tenant: str = TENANT) -> str:
    return derive(part, tenant)


HOST_TEAM = derived("team")
HOST_LOG_THREAD = derived("log_thread")
HOST_LOG_BRANCH = derived("log_branch")
BILLING_THREADS = {
    1: "0192a000-0000-7000-8000-0000000000c1",
    2: "0192a000-0000-7000-8000-0000000000c2",
}
BILLING_BRANCHES = {
    1: "0192b000-0000-7000-8000-0000000000c1",
    2: "0192b000-0000-7000-8000-0000000000c2",
}
SUPPORT_THREAD = "0192a000-0000-7000-8000-0000000000d1"
SUPPORT_BRANCH = "0192b000-0000-7000-8000-0000000000d1"
SALES_THREAD = "0192a000-0000-7000-8000-0000000000d2"
SALES_BRANCH = "0192b000-0000-7000-8000-0000000000d2"
DEADLINE = T0 + 600_000
POLICY: Obj = {"restart": "on_failure", "max_restarts": 3, "within_ms": 60_000}


def billing(generation: int = 1) -> Obj:
    return {"tenant": TENANT, "team": HOST_TEAM, "name": "billing", "generation": generation}


def caller(agent: str = "support") -> Obj:
    thread, branch = (
        (SUPPORT_THREAD, SUPPORT_BRANCH) if agent == "support" else (SALES_THREAD, SALES_BRANCH)
    )
    return {"caller": {"thread_id": thread, "branch_id": branch, "agent": agent}}


def _pin(log: Log, agent: str, tools: tuple[str, ...], extra: Obj) -> Obj:
    cfg: Obj = {
        "agent_name": agent,
        "instructions": f"You are {agent}.",
        "model": MODEL,
        "model_params": PARAMS,
        "adapter": ADAPTER,
        "tools": catalog_specs(tools),
    }
    return log.add("thread_started", {**cfg, "config_hash": sha(canonical(cfg)), **extra})


def config_hash(agent: str, tools: tuple[str, ...]) -> str:
    probe = Log(SUPPORT_BRANCH, thread=SUPPORT_THREAD)
    return text(obj(_pin(probe, agent, tools, {})["data"])["config_hash"])


BILLING_TOOLS = ("reply",)


def host_team_log(generations: int = 1) -> Log:
    """The host team log as the lazy open writes it: team_opened{kind: host} and one
    member_started{host_member} per configured host member."""
    log = Log(HOST_LOG_BRANCH, thread=HOST_LOG_THREAD)
    log.add("team_opened", {"team": HOST_TEAM, "kind": "host", "tenant": TENANT})
    if generations:
        log.add("member_started", host_start(1))
    return log


def host_start(generation: int, **more: JsonValue) -> Obj:
    return {
        "member": billing(generation),
        "agent": "billing",
        "config_hash": config_hash("billing", BILLING_TOOLS),
        "thread_id": BILLING_THREADS[generation],
        "host_member": True,
        **more,
    }


def billing_log(generation: int = 1) -> Log:
    """A host member's log, opened at materialize: a root thread, idle with no task."""
    log = Log(BILLING_BRANCHES[generation], thread=BILLING_THREADS[generation])
    member: Obj = {"team": HOST_TEAM, "name": "billing", "generation": generation}
    _pin(log, "billing", BILLING_TOOLS, {"host_member": member})
    return log


def caller_log(agent: str = "support") -> Log:
    """A caller's log: a thread in no team whose only team tool is ask (its rule allows ask)."""
    c = obj(caller(agent)["caller"])
    log = Log(text(c["branch_id"]), thread=text(c["thread_id"]))
    _pin(log, agent, ("ask",), {})
    return log


def ask_billing(  # noqa: PLR0913 - the ask's own inputs, and the principal a case forges
    log: Log, agent: str, root: str, cid: str, question: str, *, principal: Obj = ALICE
) -> Obj:
    """The caller's ask of billing, decided by the host rule, and its park. Returns the ask."""
    c = call(log, "ask", {"to": "billing", "question": question}, cid)
    rule: Obj = {"from": agent, "to": "billing"}
    decision: Obj = {"op": "ask", "decision": "allow", "source": "message_policy", "rule": rule}
    log.add("message_policy_decided", {**decision, "target": "billing", "call_id": cid})
    mail = f"{log.branch}:{cid}"
    env: Obj = {
        "mail_id": mail,
        "kind": "ask",
        "team": HOST_TEAM,
        "from": caller(agent),
        "to": {"name": "billing", "generation": 1},
        "provenance": prov(log, root, principal),
        "causal": {"thread_id": log.thread, "event_id": c["event_id"]},
        "ask_id": mail,
        "deadline": DEADLINE,
        "body": body(question),
    }
    log.add("message_sent", {"envelope": env})
    host(log, "parked", {"address": {"kind": "ask", "id": mail}, "reason": "awaiting_member"})
    return env


def prov(log: Log, root: str, principal: Obj = ALICE) -> Obj:
    return {
        "principal": principal,
        "root_request": {"thread_id": log.thread, "event_id": root},
        "via": [],
    }


def reply(log: Log, ask: Obj, cid: str, answer_text: str, generation: int = 1) -> Obj:
    """Billing's reply tool call, its reply to the caller, and the call's result."""
    c = call(log, "reply", {"ask_id": ask["ask_id"], "text": answer_text}, cid)
    env: Obj = {
        "mail_id": f"{log.branch}:{cid}",
        "kind": "reply",
        "team": HOST_TEAM,
        "from": billing(generation),
        "to": ask["from"],
        "provenance": ask["provenance"],
        "causal": {"thread_id": log.thread, "event_id": c["event_id"]},
        "ask_id": ask["ask_id"],
        "body": body(answer_text),
    }
    log.add("message_sent", {"envelope": env})
    return env


def turn_failed(log: Log, asks: list[Obj], error: Obj, turn_end: Obj, generation: int = 1) -> None:
    """A host member's failed turn: one turn_failed bounce per ask it took, then
    member_idle{turn_failed}. Only the turn ends."""
    for ask in asks:
        env: Obj = {
            "mail_id": f"{log.branch}:{eid(log.seq + 1, log.branch)}",
            "kind": "bounce",
            "team": HOST_TEAM,
            "from": billing(generation),
            "to": ask["from"],
            "provenance": ask["provenance"],
            "causal": {"thread_id": log.thread, "event_id": turn_end["event_id"]},
            "ask_id": ask["ask_id"],
            "code": "turn_failed",
            "error": error,
        }
        log.add("message_sent", {"envelope": env})
    log.add("member_idle", {"turn_failed": error})


def take(log: Log, env: Obj) -> Obj:
    return received(log, env)
