"""Finding a host team's logs and pruning a deleted caller's rows (Teams Phase 2, D.4).

A host team has no lead, so a rebuild starts from the ids its tenant derives; its members come
from the host team log's `member_started{host_member}`, and its callers — threads in no team,
whose own log may be the only record of a pending ask — from a scan of the tenant's mail events.
"""

from collections.abc import Collection, Sequence
from typing import Final

from threads.log import (
    BranchId,
    CallerAddress,
    MailEnvelope,
    MemberStartedEvent,
    MessageReceivedEvent,
    MessageSentEvent,
    TeamOpenedEvent,
    ThreadId,
    parse_log_line,
)
from threads.result import Ok
from threads.store.conn import Conn
from threads.store.sql import blob_of, text_of
from threads.store.started import Opened
from threads.team.host_team import host_team_ids
from threads.team.index import TeamLog
from threads.team.rows import bytes_of

_MAIL_TYPES: Final = ("message_sent", "message_received", "mail_refused", "ask_closed")
"""The event types a caller's log can hold of its host team's mail (the `events.type` column)."""


def host_team_branches(
    conn: Conn, tenant_id: str, team_id: str, opened: Sequence[Opened]
) -> list[TeamLog]:
    """A host team's logs: its team log at the derived branch, each host member's thread, and
    every caller of the tenant that its mail names. Empty when this tenant has no such team."""
    ids = host_team_ids(tenant_id)
    if team_id != ids.team:
        return []
    log = next((o for o in opened if o.branch_id == ids.log_branch_id and _host(o, team_id)), None)
    if log is None:
        return []
    threads = _host_member_threads(conn, ids.log_branch_id)
    logs = [TeamLog(log.thread_id, log.branch_id)]
    logs += [TeamLog(o.thread_id, o.branch_id) for o in opened if o.thread_id in threads]
    logs += _caller_logs(conn, tenant_id, team_id, {log.branch_id for log in logs})
    return sorted(logs, key=lambda log: log.branch_id)


def _host(opened: Opened, team_id: str) -> bool:
    e = opened.event
    return isinstance(e, TeamOpenedEvent) and e.data.kind == "host" and e.data.team == team_id


def _host_member_threads(conn: Conn, log_branch: BranchId) -> set[ThreadId]:
    """The thread each `member_started{host_member}` of the host team log names. A host member's
    branch is that thread's own: it has no parent to walk."""
    rows = conn.execute(
        "SELECT line FROM events WHERE branch_id = ? AND type = 'member_started'", (log_branch,)
    ).fetchall()
    started = (_parsed(line) for (line,) in rows)
    return {e.data.thread_id for e in started if isinstance(e, MemberStartedEvent)}


def _caller_logs(
    conn: Conn, tenant_id: str, team_id: str, known: Collection[BranchId]
) -> list[TeamLog]:
    """Every branch of the tenant whose own mail names it as a caller of this host team.
    ponytail: a full events scan by type, since rebuilds are rare."""
    marks = ", ".join("?" for _ in _MAIL_TYPES)
    rows = conn.execute(
        "SELECT b.thread_id, b.branch_id, e.line FROM events e"  # noqa: S608 - a fixed type list
        " JOIN branches b ON b.branch_id = e.branch_id"
        f" WHERE b.tenant_id = ? AND b.parent_branch_id IS NULL AND e.type IN ({marks})"
        " ORDER BY b.branch_id, e.seq",
        (tenant_id, *_MAIL_TYPES),
    ).fetchall()
    found: dict[str, TeamLog] = {}
    for thread, branch_id, line in rows:
        branch = BranchId(text_of(branch_id))
        if branch in known or branch in found:
            continue
        env = _envelope(_parsed(line))
        if env is not None and env.team == team_id and _names_caller(env, branch):
            found[branch] = TeamLog(ThreadId(text_of(thread)), branch)
    return list(found.values())


def _parsed(line: object) -> object:
    parsed = parse_log_line(blob_of(line).decode("utf-8", "surrogatepass"))
    return parsed.value if isinstance(parsed, Ok) else None


def _envelope(event: object) -> MailEnvelope | None:
    if isinstance(event, MessageSentEvent | MessageReceivedEvent):
        return event.data.envelope
    return None


def _names_caller(env: MailEnvelope, branch: BranchId) -> bool:
    return any(
        isinstance(end, CallerAddress) and end.caller.branch_id == branch
        for end in (env.from_, env.to)
    )


def caller_threads(env: MailEnvelope) -> set[ThreadId]:
    """The caller threads an envelope names, at either end."""
    return {end.caller.thread_id for end in (env.from_, env.to) if isinstance(end, CallerAddress)}


def drop_deleted_callers(conn: Conn, tenant_id: str, team_id: str) -> None:
    """A mail or ask naming a tombstoned caller thread is not rebuilt (R29-1), so the wiped and
    rebuilt index equals the index the delete left."""
    gone = [
        ThreadId(text_of(t))
        for (t,) in conn.execute(
            "SELECT thread_id FROM tombstones WHERE tenant_id = ?", (tenant_id,)
        ).fetchall()
    ]
    if gone:
        prune_caller_rows(conn, gone, team_id)


def prune_caller_rows(
    conn: Conn, threads: Collection[ThreadId], team_id: str | None = None
) -> None:
    """Deletes every mail row naming one of these threads as caller, and the ask it opened
    (R29-1): the deleting transaction's own cleanup, and the rebuild's. The envelope is the only
    record of a mail's sender, so the rows are read and parsed rather than filtered in SQL; only
    a host team's, since a caller's mail is always its tenant's host team's (rule 52)."""
    host = " WHERE team_id IN (SELECT team_id FROM teams WHERE kind = 'host')"
    where = host if team_id is None else " WHERE team_id = ?"
    params = () if team_id is None else (team_id,)
    rows = conn.execute(f"SELECT mail_id, envelope FROM mail{where}", params).fetchall()  # noqa: S608
    doomed = set(threads)
    for mail_id, raw in rows:
        if caller_threads(MailEnvelope.model_validate_json(bytes_of(raw))) & doomed:
            conn.execute("DELETE FROM mail WHERE mail_id = ?", (text_of(mail_id),))
            conn.execute("DELETE FROM asks WHERE ask_id = ?", (text_of(mail_id),))


def caller_busy(conn: Conn, branches: Collection[BranchId]) -> str | None:
    """Why a caller branch is not quiescent yet (R29-1): it is the asker of an open ask, holds
    an unconsumed reply or bounce, or sent mail a host member has not taken. A branch of a team
    is no caller: its own team's rules and its lead's deletion cover it."""
    for branch in branches:
        if _in_a_team(conn, branch):
            continue
        if _found(conn, "SELECT 1 FROM asks WHERE asker_branch_id = ? AND state = 'open'", branch):
            return f"branch {branch} is the asker of an open ask"
        pending = (
            "SELECT 1 FROM mail WHERE to_branch_id = ? AND to_kind = 'caller' AND state = 'pending'"
        )
        if _found(conn, pending, branch):
            return f"branch {branch} holds an unconsumed reply or bounce"
        for mail_id in _sent_from(conn, branch):
            if _found(conn, "SELECT 1 FROM mail WHERE mail_id = ? AND state = 'pending'", mail_id):
                return f"branch {branch} sent mail {mail_id} that is still pending"
    return None


def _in_a_team(conn: Conn, branch: BranchId) -> bool:
    return _found(conn, "SELECT 1 FROM team_members WHERE branch_id = ?", branch) or _found(
        conn, "SELECT 1 FROM teams WHERE team_log_branch_id = ?", branch
    )


def _sent_from(conn: Conn, branch: BranchId) -> list[str]:
    """The mail ids of a branch's own message_sent events: `mail` has no sender column."""
    rows = conn.execute(
        "SELECT line FROM events WHERE branch_id = ? AND type = 'message_sent'", (branch,)
    ).fetchall()
    sent = (_parsed(line) for (line,) in rows)
    return [e.data.envelope.mail_id for e in sent if isinstance(e, MessageSentEvent)]


def _found(conn: Conn, query: str, key: object) -> bool:
    return conn.execute(query, (key,)).fetchone() is not None
