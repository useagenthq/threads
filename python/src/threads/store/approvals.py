"""Single-use approval challenges (store.sql `approvals`).

A row is inserted in the transaction that appends its approval_requested, and consumed by one
conditional update in the transaction that appends approval_granted or approval_denied. The
row carries the bindings the log doesn't: the tenant and the channel installation.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from threads.log import ApprovalRequestedEvent, BranchId, CallId, ParseError, ThreadId
from threads.store.companion import Companion
from threads.store.conn import Conn
from threads.store.sql import int_of, text_of
from threads.store.verify import StoredEvent

type Decided = Literal["granted", "denied"]


@dataclass(frozen=True, slots=True)
class Challenge:
    challenge_id: str
    installation_id: str | None
    thread_id: ThreadId
    branch_id: BranchId
    call_id: CallId
    args_hash: str
    expires_at: int
    state: str


def record(conn: Conn, tenant_id: str, events: Sequence[StoredEvent]) -> None:
    """One open row per approval_requested of an append, bound to the thread's channel
    installation when the thread is a channel conversation's (null otherwise)."""
    for event in events:
        if not isinstance(event, ApprovalRequestedEvent):
            continue
        data = event.data
        conn.execute(
            "INSERT INTO approvals (challenge_id, tenant_id, installation_id, thread_id, branch_id,"
            " call_id, args_hash, file_hashes, expires_at, state)"
            " VALUES (?, ?, (SELECT installation_id FROM channel_threads"
            " WHERE tenant_id = ? AND thread_id = ? LIMIT 1), ?, ?, ?, ?, ?, ?, 'open')",
            # ponytail: no referenced-file hashes yet ("[]"); bind them when calls declare files.
            (
                data.challenge_id,
                tenant_id,
                tenant_id,
                event.thread_id,
                event.thread_id,
                event.branch_id,
                data.call_id,
                data.args_hash,
                b"[]",
                data.expires_at,
            ),
        )


def find(conn: Conn, tenant_id: str, challenge_id: str) -> Challenge | None:
    """The tenant's challenge; another tenant's is not found."""
    row = conn.execute(
        "SELECT installation_id, thread_id, branch_id, call_id, args_hash, expires_at, state"
        " FROM approvals WHERE challenge_id = ? AND tenant_id = ?",
        (challenge_id, tenant_id),
    ).fetchone()
    if row is None:
        return None
    installation, thread, branch, call, args, expires, state = row
    return Challenge(
        challenge_id,
        None if installation is None else text_of(installation),
        ThreadId(text_of(thread)),
        BranchId(text_of(branch)),
        CallId(text_of(call)),
        text_of(args),
        int_of(expires),
        text_of(state),
    )


def expire(conn: Conn, challenge_id: str, now: int) -> None:
    """An answer at or after the expiry moves an open row to expired: a denial."""
    conn.execute(
        "UPDATE approvals SET state = 'expired', decided_at = ?"
        " WHERE challenge_id = ? AND state = 'open' AND expires_at <= ?",
        (now, challenge_id, now),
    )


def consume(challenge_id: str, state: Decided, principal_key: str, now: int) -> Companion:
    """The conditional update that makes the answer single-use: only an open, unexpired row
    moves, so a second answer (or a race) is approval_duplicate and appends nothing."""

    def update(conn: Conn, _events: Sequence[StoredEvent]) -> ParseError | None:
        done = conn.execute(
            "UPDATE approvals SET state = ?, decided_by = ?, decided_at = ?"
            " WHERE challenge_id = ? AND state = 'open' AND expires_at > ?",
            (state, principal_key, now, challenge_id, now),
        )
        if done.rowcount == 1:
            return None
        return ParseError("approval_duplicate", f"challenge {challenge_id} is already answered")

    return update
