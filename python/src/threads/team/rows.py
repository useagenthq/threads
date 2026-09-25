"""Typed reads of the team index inside a decided append's transaction (store.sql, Teams). The
rows are a projection of logs the store already verified, so a row that fails its schema is a
broken store invariant: it raises, never a value a caller branches on."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from pydantic import JsonValue, TypeAdapter

from threads.log import MailEnvelope, StoredMemberResult
from threads.store.conn import Conn
from threads.store.sql import int_of, text_of

type Role = Literal["lead", "member"]
type State = Literal["starting", "running", "idle", "parked", "ended"]
_ROLES: dict[str, Role] = {"lead": "lead", "member": "member"}
_STATES: dict[str, State] = {
    "starting": "starting",
    "running": "running",
    "idle": "idle",
    "parked": "parked",
    "ended": "ended",
}


@dataclass(frozen=True, slots=True)
class TeamRow:
    team_id: str
    tenant_id: str
    lead_thread_id: str
    team_log_branch_id: str
    closed_at: int | None


@dataclass(frozen=True, slots=True)
class MemberRow:
    team_id: str
    name: str
    generation: int
    role: Role
    agent: str
    config_hash: str
    thread_id: str
    branch_id: str | None
    state: State


@dataclass(frozen=True, slots=True)
class MonitorRow:
    monitor_id: str
    watcher_branch_id: str
    kind: str


_RESULT: TypeAdapter[StoredMemberResult] = TypeAdapter(StoredMemberResult)
_JSON: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)
_MEMBER = "team_id, name, generation, role, agent, config_hash, thread_id, branch_id, state"


def team_row(conn: Conn, team: str) -> TeamRow | None:
    return _team(conn, "team_id", team)


def team_of_log(conn: Conn, branch: str) -> TeamRow | None:
    """The team whose log is `branch`, if it is a team log."""
    return _team(conn, "team_log_branch_id", branch)


def _team(
    conn: Conn, column: Literal["team_id", "team_log_branch_id"], value: str
) -> TeamRow | None:
    row = conn.execute(
        "SELECT team_id, tenant_id, lead_thread_id, team_log_branch_id, closed_at FROM teams"  # noqa: S608
        f" WHERE {column} = ?",
        (value,),
    ).fetchone()
    if row is None:
        return None
    team_id, tenant, lead, log, closed = row
    closed_at = None if closed is None else int_of(closed)
    return TeamRow(text_of(team_id), text_of(tenant), text_of(lead), text_of(log), closed_at)


def _member(row: tuple[object, ...]) -> MemberRow:
    team, name, gen, role, agent, config, thread, branch, state = row
    return MemberRow(
        text_of(team),
        text_of(name),
        int_of(gen),
        _ROLES[text_of(role)],
        text_of(agent),
        text_of(config),
        text_of(thread),
        None if branch is None else text_of(branch),
        _STATES[text_of(state)],
    )


def member_rows(conn: Conn, team: str) -> list[MemberRow]:
    """Every member row of the team, the lead's included, in (name, generation) order."""
    rows = conn.execute(
        f"SELECT {_MEMBER} FROM team_members WHERE team_id = ? ORDER BY name, generation",  # noqa: S608
        (team,),
    ).fetchall()
    return [_member(r) for r in rows]


def member_named(conn: Conn, team: str, name: str) -> MemberRow | None:
    """The member's current row: its highest generation."""
    rows = [r for r in member_rows(conn, team) if r.name == name]
    return rows[-1] if rows else None


def own_rows(conn: Conn, thread: str) -> list[MemberRow]:
    """The rows a thread's own events write: a member's, a lead's, both for a nested lead."""
    rows = conn.execute(
        f"SELECT {_MEMBER} FROM team_members WHERE thread_id = ? ORDER BY role, team_id",  # noqa: S608
        (thread,),
    ).fetchall()
    return [_member(r) for r in rows]


def ref_of(team: TeamRow, row: MemberRow) -> dict[str, JsonValue]:
    """A member row as the ref every API and envelope carries."""
    return {
        "tenant": team.tenant_id,
        "team": row.team_id,
        "name": row.name,
        "generation": row.generation,
    }


def _envelopes(rows: Sequence[tuple[object, ...]]) -> list[MailEnvelope]:
    return [MailEnvelope.model_validate_json(bytes_of(r[0])) for r in rows]


def bytes_of(value: object) -> bytes:
    if not isinstance(value, bytes):
        raise TypeError(f"a JSON column holds bytes, got {type(value).__name__}")
    return value


def pending_to(conn: Conn, team: str, name: str | None) -> list[MailEnvelope]:
    """Pending mail to a member's name (or to the team log), in (created_at, mail_id) order."""
    rows = conn.execute(
        "SELECT envelope FROM mail WHERE team_id = ? AND to_name IS NOT DISTINCT FROM ?"
        " AND state = 'pending'"
        " ORDER BY created_at, mail_id",
        (team, name),
    ).fetchall()
    return _envelopes(rows)


def pending_here(conn: Conn, thread: str, branch: str) -> list[MailEnvelope]:
    """Pending mail to a writer: its thread's own rows, or the team log when it is one."""
    rows = own_rows(conn, thread)
    if rows:
        return pending_for(conn, rows)
    team = team_of_log(conn, branch)
    return [] if team is None else pending_to(conn, team.team_id, None)


def pending_for(conn: Conn, rows: Sequence[MemberRow]) -> list[MailEnvelope]:
    """Pending mail to any of a thread's own rows (a nested lead has two), in one order."""
    if not rows:
        return []
    pairs = ", ".join("(?, ?)" for _ in rows)
    params = [v for r in rows for v in (r.team_id, r.name)]
    found = conn.execute(
        "SELECT envelope FROM mail WHERE state = 'pending' AND (team_id, to_name) IN"  # noqa: S608
        f" (VALUES {pairs}) ORDER BY created_at, mail_id",
        params,
    ).fetchall()
    return _envelopes(found)


def mail_envelope(conn: Conn, mail_id: str) -> MailEnvelope | None:
    """A mail row's envelope, whatever its state."""
    rows = conn.execute("SELECT envelope FROM mail WHERE mail_id = ?", (mail_id,)).fetchall()
    found = _envelopes(rows)
    return found[0] if found else None


@dataclass(frozen=True, slots=True)
class Settled:
    """An idle or ended member's committed result and the seq of the event that wrote it."""

    result: JsonValue
    seq: int


def settled_of(conn: Conn, row: MemberRow) -> Settled:
    """The member's committed result: the row's, written by its member_idle or member_ended
    (nothing else changes the row until its next turn opens)."""
    found = conn.execute(
        "SELECT result, updated_seq FROM team_members WHERE team_id = ? AND name = ?"
        " AND generation = ?",
        (row.team_id, row.name, row.generation),
    ).fetchone()
    if found is None:
        raise AssertionError(f"no settled row {row.name}")
    result = _RESULT.validate_json(bytes_of(found[0]))
    return Settled(
        _JSON.validate_python(_RESULT.dump_python(result, mode="json")), int_of(found[1])
    )


@dataclass(frozen=True, slots=True)
class AskRow:
    asker_branch_id: str
    deadline: int
    state: str


def ask_row(conn: Conn, ask_id: str) -> AskRow | None:
    """An ask's row, whatever its state."""
    found = conn.execute(
        "SELECT asker_branch_id, deadline, state FROM asks WHERE ask_id = ?", (ask_id,)
    ).fetchone()
    if found is None:
        return None
    return AskRow(text_of(found[0]), int_of(found[1]), text_of(found[2]))


def open_asks(conn: Conn, branch: str) -> list[str]:
    """This branch's open asks, in ask_id order."""
    rows = conn.execute(
        "SELECT ask_id FROM asks WHERE asker_branch_id = ? AND state = 'open' ORDER BY ask_id",
        (branch,),
    ).fetchall()
    return [text_of(a) for (a,) in rows]


def due_asks(conn: Conn, branch: str, now: int) -> list[tuple[str, int]]:
    """This branch's open asks whose deadline is at or before `now`, oldest first, with it."""
    rows = conn.execute(
        "SELECT ask_id, deadline FROM asks WHERE asker_branch_id = ? AND state = 'open'"
        " AND deadline <= ? ORDER BY deadline, ask_id",
        (branch, now),
    ).fetchall()
    return [(text_of(a), int_of(d)) for a, d in rows]


def monitors_on(conn: Conn, team: str, row: MemberRow) -> list[MonitorRow]:
    """The live monitors on one member generation, in monitor_id order."""
    rows = conn.execute(
        "SELECT monitor_id, watcher_branch_id, kind FROM monitors"
        " WHERE team_id = ? AND target_name = ? AND target_generation = ? ORDER BY monitor_id",
        (team, row.name, row.generation),
    ).fetchall()
    return [MonitorRow(text_of(m), text_of(w), text_of(k)) for m, w, k in rows]
