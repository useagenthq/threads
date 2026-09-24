"""`mail.claim` (Gate 1 §4.6): marks a pending mail row as one worker's to wake its recipient
for. A claim only deduplicates wakes: the lease holder consumes, so correctness never depends
on it, and it writes no log."""

import sqlite3
from typing import Literal

from threads.store.sql import transaction
from threads.team.constants import TEAM_CONSTANTS


def claim_mail(
    conn: sqlite3.Connection,
    mail_id: str,
    token: str,
    now: int,
    ttl_ms: int = TEAM_CONSTANTS.claim_ttl_ms,
) -> Literal["claimed", "busy"]:
    """Claims a pending row unless another worker's claim on it is still live. An expired claim
    is taken over by the same statement, so a killed worker's row is woken once after `ttl_ms`."""
    with transaction(conn):
        cursor = conn.execute(
            "UPDATE mail SET claim_token = ?, claim_expires_at = ?"
            " WHERE mail_id = ? AND state = 'pending'"
            " AND (claim_token IS NULL OR claim_expires_at <= ?)",
            (token, now + ttl_ms, mail_id, now),
        )
        return "claimed" if cursor.rowcount == 1 else "busy"
