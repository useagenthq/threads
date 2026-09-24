"""Typed reads of the team index inside a decided append's transaction (store.sql, Teams). The
rows are a projection of logs the store already verified, so a row that fails its schema is a
broken store invariant: it raises, never a value a caller branches on."""

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from pydantic import JsonValue

from threads.log import MailEnvelope
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


_MEMBER = "team_id, name, generation, role, agent, config_hash, thread_id, branch_id, state"


def team_row(conn: sqlite3.Connection, team: str) -> TeamRow | None:
    row: tuple[object, ...] | None = conn.execute(
        "SELECT team_id, tenant_id, lead_thread_id, team_log_branch_id, closed_at FROM teams"
        " WHERE team_id = ?",
        (team,),
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


def member_rows(conn: sqlite3.Connection, team: str) -> list[MemberRow]:
    """Every member row of the team, the lead's included, in (name, generation) order."""
    rows: list[tuple[object, ...]] = conn.execute(
        f"SELECT {_MEMBER} FROM team_members WHERE team_id = ? ORDER BY name, generation",  # noqa: S608
        (team,),
    ).fetchall()
    return [_member(r) for r in rows]


def member_named(conn: sqlite3.Connection, team: str, name: str) -> MemberRow | None:
    """The member's current row: its highest generation."""
    rows = [r for r in member_rows(conn, team) if r.name == name]
    return rows[-1] if rows else None


def own_rows(conn: sqlite3.Connection, thread: str) -> list[MemberRow]:
    """The rows a thread's own events write: a member's, a lead's, both for a nested lead."""
    rows: list[tuple[object, ...]] = conn.execute(
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


def pending_to(conn: sqlite3.Connection, team: str, name: str | None) -> list[MailEnvelope]:
    """Pending mail to a member's name (or to the team log), in (created_at, mail_id) order."""
    rows: list[tuple[object, ...]] = conn.execute(
        "SELECT envelope FROM mail WHERE team_id = ? AND to_name IS ? AND state = 'pending'"
        " ORDER BY created_at, mail_id",
        (team, name),
    ).fetchall()
    return _envelopes(rows)


def pending_for(conn: sqlite3.Connection, rows: Sequence[MemberRow]) -> list[MailEnvelope]:
    """Pending mail to any of a thread's own rows (a nested lead has two), in one order."""
    if not rows:
        return []
    pairs = ", ".join("(?, ?)" for _ in rows)
    params = [v for r in rows for v in (r.team_id, r.name)]
    found: list[tuple[object, ...]] = conn.execute(
        "SELECT envelope FROM mail WHERE state = 'pending' AND (team_id, to_name) IN"  # noqa: S608
        f" (VALUES {pairs}) ORDER BY created_at, mail_id",
        params,
    ).fetchall()
    return _envelopes(found)


def mail_envelope(conn: sqlite3.Connection, mail_id: str) -> MailEnvelope | None:
    """A mail row's envelope, whatever its state."""
    rows: list[tuple[object, ...]] = conn.execute(
        "SELECT envelope FROM mail WHERE mail_id = ?", (mail_id,)
    ).fetchall()
    found = _envelopes(rows)
    return found[0] if found else None


def monitors_on(conn: sqlite3.Connection, team: str, row: MemberRow) -> list[MonitorRow]:
    """The live monitors on one member generation, in monitor_id order."""
    rows: list[tuple[object, object, object]] = conn.execute(
        "SELECT monitor_id, watcher_branch_id, kind FROM monitors"
        " WHERE team_id = ? AND target_name = ? AND target_generation = ? ORDER BY monitor_id",
        (team, row.name, row.generation),
    ).fetchall()
    return [MonitorRow(text_of(m), text_of(w), text_of(k)) for m, w, k in rows]
