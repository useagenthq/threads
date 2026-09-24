"""Provenance (spec/schema/README.md, "Teams"): every mail and member_started a turn sends carries
the (principal, root_request) of that turn, read from its opener. One turn has one authority and
one budget root."""

import sqlite3
from collections.abc import Sequence

from pydantic import JsonValue
from pydantic.experimental.missing_sentinel import MISSING

from threads.log import (
    AgentSpawnedEvent,
    Event,
    MessageReceivedEvent,
    ToolResultLateEvent,
    UserInputEvent,
    WokenEvent,
)
from threads.reduce.handlers import to_json
from threads.reduce.openers import turn_start
from threads.team.rows import mail_envelope


def turn_provenance(conn: sqlite3.Connection, events: Sequence[Event]) -> JsonValue:
    """The provenance of the open turn, or of the last one; None before any turn."""
    start = turn_start(events)
    return None if start is None else _of(conn, events, events[start])


def _of(conn: sqlite3.Connection, events: Sequence[Event], e: Event) -> JsonValue:
    if isinstance(e, MessageReceivedEvent):
        return to_json(e.data.envelope.provenance)
    if isinstance(e, UserInputEvent):
        if e.data.mail_id is not MISSING:
            # A task's user_input belongs to its task mail's request.
            task = mail_envelope(conn, e.data.mail_id)
            return None if task is None else to_json(task.provenance)
        root: JsonValue = {"thread_id": e.thread_id, "event_id": e.event_id}
        return {"principal": to_json(e.actor.principal), "root_request": root, "via": []}
    if isinstance(e, WokenEvent):
        return _woken(conn, events, e)
    return None


def _woken(conn: sqlite3.Connection, events: Sequence[Event], e: WokenEvent) -> JsonValue:
    """A woken turn belongs to the run that spawned its first cause's child."""
    late = next(
        (
            x
            for x in events
            if isinstance(x, ToolResultLateEvent) and x.event_id == e.data.causes[0]
        ),
        None,
    )
    if late is None:
        return None
    spawn = next(
        (
            i
            for i, x in enumerate(events)
            if isinstance(x, AgentSpawnedEvent) and x.data.call_id == late.data.call_id
        ),
        0,
    )
    start = turn_start(events[:spawn])
    return None if start is None else _of(conn, events, events[start])
