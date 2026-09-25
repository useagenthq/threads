"""`threads delete` (spec/schema/README.md, "Deleting a thread"): the deletion set is a fixed
point, deleted in one transaction, only when nothing in it is still running. The resource ledger
is never deleted with log rows: it owns cleanup."""

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Final, Literal

from pydantic.experimental.missing_sentinel import MISSING

from threads.log import BranchId, ThreadId, ThreadStartedEvent
from threads.result import Err, Ok
from threads.store.conn import Conn
from threads.store.losses import record_losses
from threads.store.sql import export, text_of, transaction
from threads.store.started import Opened, opened_threads, owner_of
from threads.store.verify import verify_export

TEAM_TABLES: Final = (
    "teams",
    "team_members",
    "mail",
    "asks",
    "monitors",
    "operator_receipts",
    "team_feed",
)
"""Every team-keyed index table: a doomed lead's team takes all of its rows."""
_PER_BRANCH: Final = ("events", "leases", "observer_cursors", "pending_wakes", "questions")


@dataclass(frozen=True, slots=True)
class DeleteError:
    """Why nothing was deleted. not_found: no such thread of the tenant. busy: a thread of the
    set still runs (a live lease or an effect in doubt). thread_in_team: a member or team log
    whose lead isn't deleted with it."""

    code: Literal["not_found", "busy", "thread_in_team"]
    message: str


def delete_thread(
    conn: Conn, tenant_id: str, thread_id: ThreadId, now: int
) -> Ok[int] | Err[DeleteError]:
    """Deletes a thread of the tenant with everything that can't outlive it: its subagent and
    team member threads, recursively, and the team log and index rows of every team a deleted
    thread leads. Handoff targets stay. The number of threads deleted; a refusal writes nothing."""
    with transaction(conn):
        owned = conn.execute(
            "SELECT 1 FROM threads WHERE thread_id = ? AND tenant_id = ?", (thread_id, tenant_id)
        ).fetchone()
        if owned is None:
            return Err(DeleteError("not_found", f"no thread {thread_id}"))
        return _delete_set(conn, tenant_id, (thread_id,), now)


def delete_tenant(conn: Conn, tenant_id: str, now: int) -> Ok[int] | Err[DeleteError]:
    """Every thread of the tenant, as one deletion set in one transaction."""
    with transaction(conn):
        rows = conn.execute(
            "SELECT thread_id FROM threads WHERE tenant_id = ?", (tenant_id,)
        ).fetchall()
        return _delete_set(conn, tenant_id, tuple(ThreadId(text_of(t)) for (t,) in rows), now)


def _delete_set(
    conn: Conn, tenant_id: str, start: Sequence[ThreadId], now: int
) -> Ok[int] | Err[DeleteError]:
    opened = opened_threads(conn, tenant_id)
    doomed = _fixed_point(opened, start)
    existing = conn.execute(
        "SELECT thread_id FROM threads WHERE tenant_id = ?", (tenant_id,)
    ).fetchall()
    alive = {text_of(t) for (t,) in existing}
    refused = _outside_its_team(opened, doomed, alive) or _running(conn, doomed, now)
    if refused is not None:
        return Err(refused)  # decided before any write, so the refusal writes nothing
    _delete_teams(conn, tenant_id, _doomed_teams(conn, tenant_id, doomed))
    for thread in doomed:
        _delete_one(conn, tenant_id, thread, now)
    return Ok(len(doomed))


def _doomed_teams(conn: Conn, tenant_id: str, doomed: Collection[ThreadId]) -> list[str]:
    """The teams a doomed lead leads, by `teams.lead_thread_id` in this tenant (Gate 1 §4.15
    rule 2). A team id is never taken from a log: an imported team_opened could name another
    tenant's team. Filtered here, not in SQL, so a large tenant never passes SQLite's variable
    limit."""
    rows = conn.execute(
        "SELECT team_id, lead_thread_id FROM teams WHERE tenant_id = ?", (tenant_id,)
    ).fetchall()
    return [text_of(team) for team, lead in rows if text_of(lead) in doomed]


def _delete_teams(conn: Conn, tenant_id: str, teams: Collection[str]) -> None:
    for team in teams:
        for table in TEAM_TABLES:
            scope = " AND tenant_id = ?" if table == "teams" else ""
            conn.execute(
                f"DELETE FROM {table} WHERE team_id = ?{scope}",  # noqa: S608
                (team, *([tenant_id] if scope else [])),
            )


def _fixed_point(opened: Sequence[Opened], start: Sequence[ThreadId]) -> set[ThreadId]:
    """Adds every thread whose parent (subagent or team_member) is in the set, and the team log
    of every lead in the set, until nothing is added."""
    doomed = set(start)
    grew = True
    while grew:
        grew = False
        for o in opened:
            if o.thread_id not in doomed and owner_of(o) in doomed:
                doomed.add(o.thread_id)
                grew = True
    return doomed


def _outside_its_team(
    opened: Sequence[Opened], doomed: Collection[ThreadId], alive: Collection[str]
) -> DeleteError | None:
    """A team member or team log goes only with its lead, unless that lead's thread no longer
    exists in the tenant (a lead whose first line doesn't parse still exists)."""
    for o in opened:
        owner = owner_of(o)
        if o.thread_id not in doomed or owner in doomed or owner not in alive:
            continue
        e = o.event
        if isinstance(e, ThreadStartedEvent):
            parent = e.data.parent
            if parent is MISSING or parent.relation != "team_member":
                continue
            what, lead = f"thread {o.thread_id} is a member of a team", parent.thread_id
        else:
            what = f"thread {o.thread_id} is the team log of team {e.data.team}"
            lead = e.data.lead_thread_id
        message = f"{what}: delete its lead {lead}, and the whole team goes with it"
        return DeleteError("thread_in_team", message)
    return None


def _running(conn: Conn, doomed: Collection[ThreadId], now: int) -> DeleteError | None:
    """busy when a branch of the set has an unexpired lease (a live executor), an effect in
    doubt (begun or unknown), or a log that doesn't verify, which can't be proved free of one:
    deleting it would erase the only record recovery settles an effect from."""
    for thread in doomed:
        live = conn.execute(
            "SELECT l.branch_id FROM leases l JOIN branches b ON b.branch_id = l.branch_id"
            " WHERE b.thread_id = ? AND l.expires_at > ?",
            (thread, now),
        ).fetchone()
        if live is not None:
            return _busy(thread, f"branch {text_of(live[0])} holds a live lease")
        why = next((w for b in _branches(conn, thread) if (w := _unsettled(conn, b, now))), None)
        if why is not None:
            return _busy(thread, why)
    return None


def _busy(thread: ThreadId, why: str) -> DeleteError:
    return DeleteError(
        "busy",
        f"thread {thread} can't be deleted yet ({why}): cancel it, wait for it to stop, resolve any"
        " parked effect, then delete",
    )


def _branches(conn: Conn, thread: ThreadId) -> list[BranchId]:
    rows = conn.execute("SELECT branch_id FROM branches WHERE thread_id = ?", (thread,)).fetchall()
    return [BranchId(text_of(b)) for (b,) in rows]


def _unsettled(conn: Conn, branch: BranchId, now: int) -> str | None:
    """Why `branch` may still hold an effect only its log can settle, if it may."""
    log = verify_export(export(conn, branch), now)
    if isinstance(log, Err):
        return (
            f"branch {branch} doesn't verify ({log.error.code}): run `threads repair {branch}`"
            " if its tail is torn, or delete it with the version that wrote it"
        )
    doubt = any(status in ("begun", "unknown") for _, status in log.value.fold.effects.values())
    return f"branch {branch} has an effect in doubt" if doubt else None


def _delete_one(conn: Conn, tenant_id: str, thread_id: ThreadId, now: int) -> None:
    """One thread's rows: its branches' log, lease, cursor and wake rows, approvals, inbox and
    channel rows, receipts and budget rows go; its live resources move to releasing for gc; a
    tombstone and one loss row per telemetry observer record it."""
    # Before the events go: what each telemetry exporter may not have sent yet.
    record_losses(conn, tenant_id, thread_id, now)
    for branch in _branches(conn, thread_id):
        for table in _PER_BRANCH:
            conn.execute(f"DELETE FROM {table} WHERE branch_id = ?", (branch,))  # noqa: S608
        conn.execute(
            "DELETE FROM budget_ledger WHERE substr(attempt_key, 1, length(CAST(? AS TEXT))) = ?",
            (f"{branch}:", f"{branch}:"),
        )
        conn.execute(
            "UPDATE resources SET state = 'releasing' WHERE owner_branch_id = ? AND state = 'live'",
            (branch,),
        )
    for table in ("approvals", "inbox", "channel_threads", "run_receipts", "schedule_threads"):
        conn.execute(
            f"DELETE FROM {table} WHERE thread_id = ? AND tenant_id = ?",  # noqa: S608
            (thread_id, tenant_id),
        )
    # Its undecided reservations are dropped, never logged; the rows only keep their keys taken.
    conn.execute(
        "UPDATE schedule_occurrences SET state = 'retired'"
        " WHERE thread_id = ? AND tenant_id = ? AND state = 'pending'",
        (thread_id, tenant_id),
    )
    # A background child's wake row lives on its parent's branch, which may outlive it.
    conn.execute("DELETE FROM pending_wakes WHERE child_thread_id = ?", (thread_id,))
    conn.execute("DELETE FROM branches WHERE thread_id = ?", (thread_id,))
    conn.execute("DELETE FROM threads WHERE thread_id = ?", (thread_id,))
    conn.execute(
        "INSERT INTO tombstones (thread_id, tenant_id, deleted_at) VALUES (?, ?, ?)"
        " ON CONFLICT DO NOTHING",
        (thread_id, tenant_id, now),
    )
