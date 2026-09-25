# pyright: strict
"""The world a team op vector runs in (spec/conformance/vectors/team-ops.json): the team's logs,
the injected clock and limits, and the reads every op makes. Rows are always the reference index
of the logs (team_index.py), so the replay rule holds by construction."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .common import T0, arr, num, obj, text
from .jcs import canonical
from .log import Log, reduce, turn_openers
from .team_index import team_index

if TYPE_CHECKING:
    from .jcs import JsonValue, Obj

# The Teams constants (spec/schema/README.md, "Teams", "Constants"). Both runtimes pin them under
# these names and let tests inject other values.
CONSTANTS: Obj = {
    "inline_cap_bytes": 16 * 1024,
    "busy_bound_ms": 5_000,
    "claim_ttl_ms": 30_000,
    "ask_wait_default_ms": 120_000,
    "wake_poll_in_process_ms": 250,
    "wake_poll_cross_process_ms": 1_000,
    "setup_attempts": 5,
}
DEFAULT_MS = num(CONSTANTS["ask_wait_default_ms"])
TEAM_LOG = "team_log"


class Refused(Exception):  # noqa: N818 - a refusal, raised only inside one op
    """An op's precondition failed: the op records `code` (and a start's `detail`) as its
    refusal."""

    def __init__(self, code: str, detail: Obj | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


@dataclass(slots=True)
class World:
    logs: dict[str, Log]
    now: int
    mailbox: int = 100
    concurrent: int = 4
    agents: tuple[str, ...] = ("researcher", "writer")  # the agents the team lists
    templates: dict[str, Obj] = field(default_factory=dict[str, "Obj"])
    """The dynamic agents the team lists: {tools: choosable in pinned order, models: keys}."""
    headroom: bool = True
    stamp: bool = True  # False while building a vector's world: builder times, not the clock
    appended: dict[str, list[str]] = field(default_factory=dict[str, list[str]])

    def index(self) -> Obj:
        return team_index(list(self.logs.values()))

    def rows(self, table: str) -> list[Obj]:
        return [obj(r) for r in arr(self.index()[table])]

    def add(
        self, label: str, type_: str, data: Obj, actor: str = "host", who: Obj | None = None
    ) -> Obj:
        """Append one event to label's log, stamped with the injected clock."""
        log = self.logs[label]
        at = self.now if self.stamp else T0 + (log.seq + 1) * 1000
        if who is None:
            e = log.add(type_, data, actor=actor, time=at)
        else:
            e = log.add(type_, data, actor=actor, principal=who, time=at)
        self.appended.setdefault(label, []).append(type_)
        return e

    # ---------- members ----------
    def team(self) -> Obj:
        return self.rows("teams")[0]

    def member(self, name: str) -> Obj | None:
        """The member's current row: its highest generation."""
        rows = [r for r in self.rows("team_members") if r["name"] == name]
        return max(rows, key=lambda r: num(r["generation"])) if rows else None

    def own_row(self, label: str) -> Obj | None:
        return next(
            (r for r in self.rows("team_members") if r["thread_id"] == self.logs[label].thread),
            None,
        )

    def ref(self, row: Obj) -> Obj:
        return {
            "tenant": self.team()["tenant_id"],
            "team": row["team_id"],
            "name": row["name"],
            "generation": row["generation"],
        }

    def caller(self, label: str) -> Obj:
        row = self.own_row(label)
        if row is None:
            raise AssertionError(f"{label} is not a member")
        return self.ref(row)

    def label_of(self, branch: str) -> str:
        return next(k for k, log in self.logs.items() if log.branch == branch)

    def address(self, branch: str) -> str:
        """Where mail to the writer of branch goes: a member's name, or the team log."""
        row = next((r for r in self.rows("team_members") if r["branch_id"] == branch), None)
        return TEAM_LOG if row is None else text(row["name"])

    def is_team_log(self, label: str) -> bool:
        return self.logs[label].events[0]["type"] == "team_opened"

    def bound(self, label: str, name: str) -> int | None:
        """The generation label's own log last recorded for name; the current row's otherwise."""
        seen: int | None = None
        for e in self.logs[label].events:
            d = obj(e["data"])
            if e["type"] == "member_started" and obj(d["member"])["name"] == name:
                seen = num(obj(d["member"])["generation"])
            elif e["type"] == "message_received":
                sender = obj(obj(d["envelope"])["from"])
                if sender.get("name") == name:
                    seen = num(sender["generation"])
        row = self.member(name)
        return seen if seen is not None else (num(row["generation"]) if row else None)

    def starter(self, row: Obj) -> str | None:
        """The label whose log holds the member's member_started."""
        for label, log in self.logs.items():
            for e in log.events:
                if (
                    e["type"] == "member_started"
                    and obj(obj(e["data"])["member"])["name"] == row["name"]
                ):
                    return label
        return None

    def settled_at(self, row: Obj) -> Obj:
        """The member's last member_idle or member_ended: its result and where it is."""
        log = self.logs[self.label_of(text(row["branch_id"]))]
        e = next(e for e in reversed(log.events) if e["type"] in ("member_idle", "member_ended"))
        source: Obj = {"thread_id": log.thread, "branch_id": log.branch, "seq": e["seq"]}
        return {"result": obj(e["data"])["result"], "source": source}

    def park_reason(self, row: Obj) -> str:
        log = self.logs[self.label_of(text(row["branch_id"]))]
        e = next(e for e in reversed(log.events) if e["type"] == "parked")
        return text(obj(e["data"])["reason"])

    # ---------- the caller's log ----------
    def state(self, label: str) -> Obj:
        """What label's reducer holds now: open turn, parks, pending calls."""
        return reduce(self.logs[label], self.now)

    def turn_provenance(self, label: str) -> Obj:
        """The (principal, root_request) of label's open or last turn, as provenance."""
        log = self.logs[label]
        opened = turn_openers(log.events)
        return self._opener(
            label, next(e for e in reversed(log.events) if text(e["event_id"]) in opened)
        )

    def _opener(self, label: str, e: Obj) -> Obj:
        """A turn opener's provenance. A woken turn belongs to the run that spawned its
        children: the provenance of the turn in which the first cause's agent_spawned was."""
        log = self.logs[label]
        d = obj(e["data"])
        if e["type"] == "message_received":
            return obj(obj(d["envelope"])["provenance"])
        if e["type"] == "user_input" and d["source"] == "team_task":
            mail = next(m for m in self.rows("mail") if m["mail_id"] == d["mail_id"])
            return obj(obj(mail["envelope"])["provenance"])
        if e["type"] == "user_input":
            root: Obj = {"thread_id": log.thread, "event_id": e["event_id"]}
            return {"principal": obj(e["actor"])["principal"], "root_request": root, "via": []}
        if e["type"] == "woken":
            late = next(x for x in log.events if x["event_id"] == arr(d["causes"])[0])
            call = obj(late["data"])["call_id"]
            spawn = next(
                i for i, x in enumerate(log.events)
                if x["type"] == "agent_spawned" and obj(x["data"])["call_id"] == call
            )  # fmt: skip
            opened = turn_openers(log.events[:spawn])
            return self._opener(
                label,
                next(x for x in reversed(log.events[:spawn]) if text(x["event_id"]) in opened),
            )
        raise AssertionError(f"no team provenance for a turn opened by {e['type']}")

    def call(self, label: str, call_id: str, args: Obj) -> Obj:
        """The pending tool_call the op dispatches."""
        e = next(
            e
            for e in self.logs[label].events
            if e["type"] == "tool_call" and obj(e["data"])["call_id"] == call_id
        )
        if obj(e["data"])["input"] != args or call_id not in arr(
            self.state(label)["pending_calls"]
        ):
            raise AssertionError(f"{call_id} is not a pending call with these args")
        return e

    def answer(self, label: str, call_id: str, value: Obj) -> Obj:
        """The call's one tool_result: its value as RFC 8785 JSON."""
        data: Obj = {
            "call_id": call_id,
            "is_error": False,
            "completeness": "complete",
            "preview": canonical(value).decode(),
            "origin": "executed",
        }
        return self.add(label, "tool_result", data, actor="tool")

    def pending(self, to: str) -> list[Obj]:
        """Pending mail to a member's name or the team log, in (created_at, mail_id) order."""
        name = None if to == TEAM_LOG else to
        rows = [m for m in self.rows("mail") if m["state"] == "pending" and m["to_name"] == name]
        return sorted(rows, key=lambda m: (num(m["created_at"]), text(m["mail_id"])))


def public(result: JsonValue) -> Obj:
    """A stored MemberResult as the public one: an inline output is its text."""
    r = obj(result)
    if r["status"] == "completed":
        return {**r, "output": obj(r["output"])["text"]}
    return r
