"""`mail.claim` (Gate 1 §4.6): marks a pending mail row as one worker's to wake its recipient
for. A claim only deduplicates wakes: the lease holder consumes, so correctness never depends
on it, and it writes no log."""

from typing import Literal

from threads.store.conn import Conn
from threads.store.sql import transaction
from threads.team.constants import TEAM_CONSTANTS

type Claim = Literal["claimed", "taken", "not_pending", "not_found"]
"""What a claim found: `claimed`, or why not. The spec's outcome for each of the others is `busy`
(leave the row); they are told apart for the worker's log. `taken`: another worker's claim on
the pending row is still live. `not_pending`: the row was consumed, refused or returned.
`not_found`: no mail row has the id."""


def claim_mail(
    conn: Conn,
    mail_id: str,
    token: str,
    now: int,
    ttl_ms: int = TEAM_CONSTANTS.claim_ttl_ms,
) -> Claim:
    """Claims a pending row unless another worker's claim on it is still live. An expired claim
    is taken over by the same statement, so a killed worker's row is woken once after `ttl_ms`."""
    with transaction(conn):
        cursor = conn.execute(
            "UPDATE mail SET claim_token = ?, claim_expires_at = ?"
            " WHERE mail_id = ? AND state = 'pending'"
            " AND (claim_token IS NULL OR claim_expires_at <= ?)",
            (token, now + ttl_ms, mail_id, now),
        )
        if cursor.rowcount == 1:
            return "claimed"
        row = conn.execute("SELECT state FROM mail WHERE mail_id = ?", (mail_id,)).fetchone()
    if row is None:
        return "not_found"
    return "taken" if row[0] == "pending" else "not_pending"
