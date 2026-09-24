"""Mail envelopes (spec/schema/README.md, "Teams", Mail): what a sender's message_sent records and
every receipt copies byte for byte."""

import sqlite3
from collections.abc import Callable

from pydantic import JsonValue

from threads.log import MailEnvelope
from threads.reduce.handlers import to_json
from threads.store.lines import Draft
from threads.team.constants import TEAM_CONSTANTS
from threads.team.rows import member_rows

type PutText = Callable[[str], JsonValue]
"""Stores text in the content-addressed store before the append that names it: its ArtifactRef."""


def address_of(conn: sqlite3.Connection, team: str, branch: str) -> JsonValue:
    """The mail's `to`: the member whose branch it is, at its generation, or the team log."""
    row = next((r for r in member_rows(conn, team) if r.branch_id == branch), None)
    return "team_log" if row is None else {"name": row.name, "generation": row.generation}


def body_of(text: str, put: PutText, cap: int = TEAM_CONSTANTS.inline_cap_bytes) -> JsonValue:
    """A text body: inline up to the inline cap, else an artifact ref written before the append."""
    return {"ref": put(text)} if len(text.encode()) > cap else {"text": text}


def sent(envelope: dict[str, JsonValue]) -> Draft:
    """message_sent{envelope}, from the sending writer."""
    return Draft("message_sent", {"envelope": envelope})


def received(envelope: MailEnvelope) -> Draft:
    """message_received{mail_id, envelope}: its actor is the mail's provenance principal."""
    actor: dict[str, JsonValue] = {
        "kind": "host",
        "principal": to_json(envelope.provenance.principal),
    }
    data: dict[str, JsonValue] = {"mail_id": envelope.mail_id, "envelope": to_json(envelope)}
    return Draft("message_received", data, actor)


def refused(mail_id: str) -> Draft:
    """mail_refused{mail_id, code}: the recipient's writer returns pending mail."""
    return Draft("mail_refused", {"mail_id": mail_id, "code": "member_ended"})
