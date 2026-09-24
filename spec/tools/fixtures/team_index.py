# pyright: strict
"""Reference replay of the team index (spec/schema/README.md, "Teams", the replay rule): the
rows of store.sql's team tables, rebuilt from a team's logs alone. Inserts come first, from
every log, then each recipient's and member's own changes; so the result doesn't depend on the
order the logs are read in."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import arr, num, obj, text
from .jcs import JsonValue
from .log import turn_openers

if TYPE_CHECKING:
    from .jcs import Obj
    from .log import Log

# A member's own events that write its row's state.
STATES = {"parked": "parked", "resumed": "running", "member_idle": "idle", "member_ended": "ended"}


def principal_key(p: Obj) -> str:
    """issuer/tenant/subject, each with % then / escaped."""
    parts = (
        text(p[k]).replace("%", "%25").replace("/", "%2F") for k in ("issuer", "tenant", "subject")
    )
    return "/".join(parts)


class _Index:
    def __init__(self) -> None:
        self.teams: dict[str, Obj] = {}
        self.members: dict[tuple[str, str, int], Obj] = {}
        self.mail: dict[str, Obj] = {}
        self.asks: dict[str, Obj] = {}
        self.monitors: dict[str, Obj] = {}
        self.receipts: dict[tuple[str, str, str, str], Obj] = {}

    # ---------- pass 1: rows a sender's or starter's append inserts ----------
    def insert(self, log: Log, e: Obj) -> None:
        d, t = obj(e["data"]), text(e["type"])
        if t == "team_opened":
            lead = obj(d["lead"])
            self.teams[text(d["team"])] = {
                "team_id": d["team"],
                "tenant_id": lead["tenant"],
                "lead_thread_id": d["lead_thread_id"],
                "team_log_branch_id": log.branch,
                "closed_at": None,
            }
        elif t == "thread_started" and "team" in d:
            lead: Obj = {"team": obj(d["team"])["id"], "name": d["agent_name"], "generation": 1}
            self._row(lead, "lead", e, log)
        elif t == "member_started":
            m = obj(d["member"])
            self._row(m, "member", e, None)
            self._monitor(f"{log.branch}:{e['event_id']}:task", log, m, "task")
        elif t == "message_sent":
            self._mail(log, e, obj(d["envelope"]))
        elif t == "operator_request" and "idempotency_key" in d:
            self._receipt(log, d)
        elif t == "wait_started":
            for m in arr(d["members"]):
                mid = f"{log.branch}:{e['event_id']}:{text(obj(m)['name'])}"
                self._monitor(mid, log, obj(m), "settle", d["wait_id"])
        elif t == "monitor_set":
            m = obj(d["member"])
            self._monitor(f"{log.branch}:{e['event_id']}:{text(m['name'])}", log, m, "end")

    def _row(self, member: Obj, role: str, e: Obj, log: Log | None) -> Obj:
        """A member's row: the lead's from its own thread_started, a member's from member_started
        (in the starter's log, before the member has a branch)."""
        d = obj(e["data"])
        row: Obj = {
            "team_id": member["team"],
            "name": member["name"],
            "generation": member["generation"],
            "role": role,
            "agent": d.get("agent", d.get("agent_name")),
            "config_hash": d["config_hash"],
            "thread_id": log.thread if log else d["thread_id"],
            "branch_id": log.branch if log else None,
            "provenance": d.get("provenance"),
            "state": "running" if log else "starting",
            "result": None,
            "updated_seq": e["seq"],
        }
        key = (text(member["team"]), text(member["name"]), num(member["generation"]))
        self.members[key] = row
        return row

    def _monitor(self, mid: str, log: Log, m: Obj, kind: str, wait: JsonValue = None) -> None:
        self.monitors[mid] = {
            "monitor_id": mid,
            "team_id": m["team"],
            "watcher_branch_id": log.branch,
            "target_name": m["name"],
            "target_generation": m["generation"],
            "kind": kind,
            "wait_id": wait,
        }

    def _receipt(self, log: Log, d: Obj) -> None:
        opened = obj(log.events[0]["data"])
        tenant, team = obj(opened["lead"])["tenant"], opened["team"]
        key = (text(tenant), text(team), text(d["op"]), text(d["idempotency_key"]))
        self.receipts[key] = {
            "tenant_id": tenant,
            "team_id": team,
            "op": d["op"],
            "idempotency_key": d["idempotency_key"],
            "principal_key": principal_key(obj(d["principal"])),
            "body_hash": d["body_hash"],
            "request_id": d["request_id"],
        }

    def _mail(self, log: Log, e: Obj, env: Obj) -> None:
        to, prov = env["to"], obj(env["provenance"])
        root = obj(prov["root_request"])
        self.mail[text(env["mail_id"])] = {
            "mail_id": env["mail_id"],
            "team_id": env["team"],
            "kind": env["kind"],
            "to_name": obj(to)["name"] if isinstance(to, dict) else None,
            "to_generation": obj(to)["generation"] if isinstance(to, dict) else None,
            "principal_key": principal_key(obj(prov["principal"])),
            "root_request": f"{text(root['thread_id'])}:{text(root['event_id'])}",
            "envelope": env,
            "created_at": e["time"],
            "state": "pending",
            "consumed_seq": None,
        }
        if env["kind"] == "ask":
            self.asks[text(env["ask_id"])] = {
                "ask_id": env["ask_id"],
                "team_id": env["team"],
                "asker_branch_id": log.branch,
                "recipient_name": obj(to)["name"],
                "recipient_generation": obj(to)["generation"],
                "deadline": env["deadline"],
                "state": "open",
                "closed_seq": None,
            }

    # ---------- pass 2: what each recipient and member changes in its own log ----------
    def change(self, log: Log) -> None:
        row = self._own_row(log)
        opened = turn_openers(log.events)
        for e in log.events:
            d, t, seq = obj(e["data"]), text(e["type"]), e["seq"]
            self._moves(d, t, seq)
            if row is None:
                continue
            state = "running" if text(e["event_id"]) in opened else STATES.get(t)
            if t == "thread_started" and "parent" in d:
                row.update(branch_id=log.branch, state="running", updated_seq=seq)
            elif state is not None:
                row.update(state=state, updated_seq=seq)
            if t in ("member_idle", "member_ended"):
                row["result"] = d["result"]
            if t == "member_ended" and row["role"] == "lead":
                self.teams[text(row["team_id"])]["closed_at"] = e["time"]

    def _moves(self, d: Obj, t: str, seq: JsonValue) -> None:
        if t == "message_received" or (t == "user_input" and d["source"] == "team_task"):
            self.mail[text(d["mail_id"])].update(state="consumed", consumed_seq=seq)
        elif t == "mail_refused":
            gone = "stale" if d["code"] == "stale_member" else "returned"
            self.mail[text(d["mail_id"])].update(state=gone, consumed_seq=seq)
        elif t == "ask_closed":
            self.asks[text(d["ask_id"])].update(state=obj(d["outcome"])["status"], closed_seq=seq)
        elif t == "message_sent":
            env = obj(d["envelope"])
            if "monitor_id" in env and env["kind"] != "member_parked":
                self.monitors.pop(text(env["monitor_id"]), None)
        elif t == "member_observed":
            self.monitors.pop(text(d["monitor_id"]), None)
        elif t == "wait_finished":
            for mid in [k for k, m in self.monitors.items() if m["wait_id"] == d["wait_id"]]:
                del self.monitors[mid]

    def _own_row(self, log: Log) -> Obj | None:
        first = obj(log.events[0]["data"])
        if log.events[0]["type"] == "team_opened":
            return None
        if "team" in first:
            return self.members[(text(obj(first["team"])["id"]), text(first["agent_name"]), 1)]
        return next((r for r in self.members.values() if r["thread_id"] == log.thread), None)


def team_index(logs: list[Log]) -> Obj:
    """The team tables' rows (claim columns excluded: they rebuild as null), sorted by key."""
    ix = _Index()
    for log in logs:
        for e in log.events:
            ix.insert(log, e)
    for log in logs:
        ix.change(log)
    return {
        "teams": list[JsonValue](ix.teams[k] for k in sorted(ix.teams)),
        "team_members": list[JsonValue](ix.members[k] for k in sorted(ix.members)),
        "mail": list[JsonValue](ix.mail[k] for k in sorted(ix.mail)),
        "asks": list[JsonValue](ix.asks[k] for k in sorted(ix.asks)),
        "monitors": list[JsonValue](ix.monitors[k] for k in sorted(ix.monitors)),
        "operator_receipts": list[JsonValue](ix.receipts[k] for k in sorted(ix.receipts)),
        "team_feed": feed_members(logs),
        "pending_wakes": pending_wakes(logs),
    }


def feed_members(logs: list[Log]) -> list[JsonValue]:
    """The team feed's rows without their epoch and offset: a rebuilt feed starts a new epoch and
    assigns offsets in delivery order, so only which events it holds is replayable."""
    rows = sorted((log.branch, num(e["seq"])) for log in logs for e in log.events)
    return [{"branch_id": b, "seq": q} for b, q in rows]


def pending_wakes(logs: list[Log]) -> list[JsonValue]:
    """One row per running background child: its agent_spawned{background} inserts it, and its
    agent_finished or the parent's parked{kind: child} deletes it."""
    rows: set[tuple[str, str]] = set()
    for log in logs:
        for e in log.events:
            d, t = obj(e["data"]), e["type"]
            if t == "agent_spawned" and d["mode"] == "background":
                rows.add((log.branch, text(d["child_thread_id"])))
            elif t == "agent_finished":
                rows.discard((log.branch, text(d["child_thread_id"])))
            elif t == "parked" and obj(d["address"])["kind"] == "child":
                rows.discard((log.branch, text(obj(d["address"])["id"])))
    return [{"branch_id": b, "child_thread_id": c} for b, c in sorted(rows)]
