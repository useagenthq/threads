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


def opened_tenant(d: Obj) -> JsonValue:
    """The tenant a team_opened indexes its team under: its lead's, or a host team's own."""
    return obj(d["lead"])["tenant"] if "lead" in d else d["tenant"]


def address_columns(to: JsonValue) -> Obj:
    """mail's to_kind and its columns, from the envelope's `to` (store.sql, mail)."""
    if not isinstance(to, dict):
        return {"to_kind": "team_log", "to_name": None, "to_generation": None, "to_branch_id": None}
    if "caller" in to:
        branch = obj(to["caller"])["branch_id"]
        return {"to_kind": "caller", "to_name": None, "to_generation": None, "to_branch_id": branch}
    member = {"to_name": to["name"], "to_generation": to["generation"]}
    return {"to_kind": "member", **member, "to_branch_id": None}


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
            self.teams[text(d["team"])] = {
                "team_id": d["team"],
                "tenant_id": opened_tenant(d),
                "kind": d.get("kind", "lead"),
                "lead_thread_id": d.get("lead_thread_id"),
                "team_log_branch_id": log.branch,
                "closed_at": None,
            }
        elif t == "thread_started" and "team" in d:
            lead: Obj = {"team": obj(d["team"])["id"], "name": d["agent_name"], "generation": 1}
            self._row(lead, "lead", e, log)
        elif t == "member_started" and d.get("host_member") is True:
            self._row(obj(d["member"]), "host_member", e, None)  # no task, no task monitor
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
        tenant, team = opened_tenant(opened), opened["team"]
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
            **address_columns(to),
            "principal_key": principal_key(obj(prov["principal"])),
            "root_request": f"{text(root['thread_id'])}:{text(root['event_id'])}",
            "envelope": env,
            "created_at": e["time"],
            "state": "pending",
            "consumed_seq": None,
        }
        if env["kind"] == "ask" and "caller" not in obj(to):
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
        rows = self.own_rows(log)
        opened = turn_openers(log.events)
        for e in log.events:
            d, t, seq = obj(e["data"]), text(e["type"]), e["seq"]
            self._moves(d, t, seq)
            for row in rows:
                self._state(row, log, e, opened)

    def _state(self, row: Obj, log: Log, e: Obj, opened: set[str]) -> None:
        """One of the log's own rows follows its events (a nested lead has two: its member row
        in the parent team and its lead row in its own)."""
        d, t, seq = obj(e["data"]), text(e["type"]), e["seq"]
        state = "running" if text(e["event_id"]) in opened else STATES.get(t)
        if t == "thread_started" and "host_member" in d:  # a host member opens no task turn
            row.update(branch_id=log.branch, state="idle", updated_seq=seq)
        elif t == "thread_started" and "parent" in d:
            row.update(branch_id=log.branch, state="running", updated_seq=seq)
        elif state is not None:
            row.update(state=state, updated_seq=seq)
        if t in ("member_idle", "member_ended") and "result" in d:  # turn_failed keeps it
            row["result"] = d["result"]
        if t == "member_ended" and row["role"] == "lead":
            self.teams[text(row["team_id"])]["closed_at"] = e["time"]

    def _moves(self, d: Obj, t: str, seq: JsonValue) -> None:
        # An update of a row no log inserted (its sender, a deleted caller, is gone) changes
        # nothing, as SQL's UPDATE does.
        if t == "message_received" or (t == "user_input" and d["source"] == "team_task"):
            self.mail.get(text(d["mail_id"]), {}).update(state="consumed", consumed_seq=seq)
        elif t == "mail_refused":
            gone = "stale" if d["code"] == "stale_member" else "returned"
            self.mail.get(text(d["mail_id"]), {}).update(state=gone, consumed_seq=seq)
        elif t == "ask_closed":
            status = obj(d["outcome"])["status"]
            self.asks.get(text(d["ask_id"]), {}).update(state=status, closed_seq=seq)
        elif t == "message_sent":
            env = obj(d["envelope"])
            if "monitor_id" in env and env["kind"] != "member_parked":
                self.monitors.pop(text(env["monitor_id"]), None)
        elif t == "member_observed":
            self.monitors.pop(text(d["monitor_id"]), None)
        elif t == "wait_finished":
            for mid in [k for k, m in self.monitors.items() if m["wait_id"] == d["wait_id"]]:
                del self.monitors[mid]

    def own_rows(self, log: Log) -> list[Obj]:
        """The rows whose thread this log is: a lead's own row, a member's row, or both for a
        nested lead. A team log has none."""
        return [r for r in self.members.values() if r["thread_id"] == log.thread]

    def feed_teams(self, log: Log) -> list[str]:
        """Every team whose feed holds this log's events: the teams its rows belong to, or the
        team whose log it is."""
        teams = {text(r["team_id"]) for r in self.own_rows(log)}
        teams |= {k for k, t in self.teams.items() if t["team_log_branch_id"] == log.branch}
        return sorted(teams)


def team_index(logs: list[Log], deleted: frozenset[str] = frozenset()) -> Obj:
    """The team tables' rows (claim columns excluded: they rebuild as null), sorted by key. A mail
    or ask naming a caller thread in `deleted` (tombstoned, Teams Phase 2) is not rebuilt."""
    ix = _Index()
    for log in logs:
        for e in log.events:
            ix.insert(log, e)
    for log in logs:
        ix.change(log)
    gone = [k for k, m in ix.mail.items() if _names_caller(obj(m["envelope"]), deleted)]
    for k in gone:
        del ix.mail[k]
        ix.asks.pop(k, None)
    return {
        "teams": list[JsonValue](ix.teams[k] for k in sorted(ix.teams)),
        "team_members": list[JsonValue](ix.members[k] for k in sorted(ix.members)),
        "mail": list[JsonValue](ix.mail[k] for k in sorted(ix.mail)),
        "asks": list[JsonValue](ix.asks[k] for k in sorted(ix.asks)),
        "monitors": list[JsonValue](ix.monitors[k] for k in sorted(ix.monitors)),
        "operator_receipts": list[JsonValue](ix.receipts[k] for k in sorted(ix.receipts)),
        "team_feed": feed_members(logs, ix),
        "pending_wakes": pending_wakes(logs),
    }


def _names_caller(env: Obj, threads: frozenset[str]) -> bool:
    ends = (env["from"], env["to"])
    return any(
        isinstance(x, dict) and obj(x.get("caller", {})).get("thread_id") in threads for x in ends
    )


def feed_members(logs: list[Log], ix: _Index) -> list[JsonValue]:
    """The team feed's rows without their epoch and offset: a rebuilt feed starts a new epoch and
    assigns offsets in delivery order, so only which events each team's feed holds is replayable.
    A nested lead's events are in both teams' feeds."""
    rows = sorted(
        (team, log.branch, num(e["seq"]))
        for log in logs
        for team in ix.feed_teams(log)
        for e in log.events
    )
    return [{"team_id": t, "branch_id": b, "seq": q} for t, b, q in rows]


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
