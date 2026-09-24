# pyright: strict
"""Steps the staged team cases share: team tool calls, starts, materialize, receipts and the
`team` case writer."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import ALICE, NOW, eid, text
from .jcs import JsonValue, canonical
from .log import reduce
from .pieces import answer, call, case, dump, result
from .team_index import team_index
from .team_pieces import (
    LEAD,
    LEAD_BRANCH,
    LEAD_THREAD,
    MEMBER_BRANCH,
    MEMBER_THREAD,
    Route,
    at,
    body,
    config_hash,
    envelope,
    member_log,
    provenance,
)

if TYPE_CHECKING:
    import pathlib

    from .jcs import Obj
    from .log import Log

FAM = "agents_teams"


def host(log: Log, type_: str, data: Obj) -> Obj:
    return log.add(type_, data, actor="host", principal=ALICE)


def tool(log: Log, name: str, inp: Obj, cid: str, target: str) -> Obj:
    """A team tool call the team allows: wait is decided as monitor."""
    c = call(log, name, inp, cid)
    op = "monitor" if name == "wait" else name
    decision: Obj = {"op": op, "decision": "allow", "source": "team", "target": target}
    log.add("message_policy_decided", {**decision, "call_id": cid})
    return c


def answered(log: Log, cid: str, value: Obj) -> Obj:
    """A team tool's result: its value as RFC 8785 JSON."""
    return result(log, cid, canonical(value).decode())


def idle(log: Log, member: Obj, answer_text: str) -> Obj:
    """The task's answer, then member_idle with it."""
    answer(log, answer_text)
    done: Obj = {"member": member, "status": "completed", "output": body(answer_text)}
    log.add("member_idle", {"result": done})
    return done


def received(log: Log, env: Obj) -> Obj:
    return host(log, "message_received", {"mail_id": env["mail_id"], "envelope": env})


def start(
    log: Log, root_event: str, member: Obj, cid: str, member_thread: str = MEMBER_THREAD
) -> tuple[str, Obj]:
    """The lead's start call: its member_started, task mail and result. Returns the member_started
    event id and the task envelope."""
    agent = text(member["name"]).rsplit("-", 1)[0]
    c = tool(log, "start", {"agent": agent, "task": f"Your task, {agent}."}, cid, agent)
    started_id = eid(log.seq + 1, LEAD_BRANCH)
    parent: Obj = {
        "thread_id": LEAD_THREAD,
        "branch_id": LEAD_BRANCH,
        "event_id": started_id,
        "relation": "team_member",
    }
    log.add(
        "member_started",
        {
            "member": member,
            "agent": agent,
            "config_hash": config_hash(agent),
            "thread_id": member_thread,
            "parent": parent,
            "provenance": provenance(root_event),
        },
    )
    route = Route(LEAD, text(member["name"]), provenance(root_event))
    task = envelope(
        f"{LEAD_BRANCH}:{cid}",
        "task",
        route,
        at(text(c["event_id"])),
        body=body(f"Your task, {agent}."),
    )
    log.add("message_sent", {"envelope": task})
    answered(log, cid, {"member": member, "status": "started"})
    return started_id, task


def materialize(
    started_id: str,
    task: Obj,
    agent: str = "researcher",
    where: tuple[str, str] = (MEMBER_BRANCH, MEMBER_THREAD),
) -> Log:
    """The member's log opened with its task as the first input."""
    log = member_log(started_id, agent, where)
    text_ = text(task["body"]["text"]) if isinstance(task["body"], dict) else ""
    host(log, "user_input", {"source": "team_task", "text": text_, "mail_id": task["mail_id"]})
    return log


def write_team(
    root: pathlib.Path, name: str, desc: str, logs: dict[str, Log], error: Obj | None = None
) -> None:
    """A `team` case: each log's export under logs/, then every log's reduced state and the team
    index its logs rebuild, or the error the logs hold."""
    d = root / name
    (d / "logs").mkdir(parents=True)
    for label, log in logs.items():
        (d / "logs" / f"{label}.jsonl").write_bytes(log.export())
    meta = case(name, FAM, "team", desc, input={"logs": list[JsonValue](logs)})
    expected: Obj = (
        {"outcome": "error", "error": error}
        if error
        else {
            "outcome": "ok",
            "states": {label: reduce(log, NOW) for label, log in logs.items()},
            "index": team_index(list(logs.values())),
        }
    )
    (d / "case.json").write_text(dump(meta))
    (d / "expected.json").write_text(dump(expected))
