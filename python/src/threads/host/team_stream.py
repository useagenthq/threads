"""`GET /v1/teams/{team}/events` (spec/schema/host-api/openapi.json subscribeTeam): a lead
team's feed as server-sent events. A pure read of team_feed: it never writes and never drives
the team. The route always follows, and closes on epoch_restarted so the client reconnects with
the new cursor."""

from collections.abc import AsyncIterator
from typing import assert_never

from pydantic import JsonValue

from threads.agents.store import Store, open_store
from threads.agents.team_feed import cursor_against, feed_head, team_events
from threads.agents.team_handle_types import (
    EpochRestarted,
    MemberSource,
    OperatorSource,
    TeamCursor,
    TeamEvent,
    TeamItem,
    TeamLogSource,
)
from threads.log import ParseError
from threads.reduce.handlers import to_json
from threads.result import Err, Ok
from threads.store.conn import Conn
from threads.store.sql import text_of

KEEPALIVE_S = 15.0
""": keepalive every 15 s keeps proxies open; it moves no cursor. Tests inject less."""


def _team_row(conn: Conn, team: str) -> tuple[str, str] | None:
    row = conn.execute("SELECT tenant_id, kind FROM teams WHERE team_id = ?", (team,)).fetchone()
    return None if row is None else (text_of(row[0]), text_of(row[1]))


async def subscribe_team(
    store: Store, team: str, tenant: str, after: TeamCursor | None
) -> Ok[AsyncIterator[TeamItem]] | Err[ParseError]:
    """The lead team's items from `after`, following. Another tenant's team, and the tenant's
    host team, are not_found: a host team has no HTTP stream (29B decision 2)."""
    sq = await open_store(store)
    row = await sq.run(lambda c: _team_row(c, team))
    if row is None or row[0] != tenant or row[1] != "lead":
        return Err(ParseError("not_found", f"no lead team {team} in {tenant}"))
    head = await feed_head(sq, team)
    if head is not None and after is not None and cursor_against(head, after) == "invalid_cursor":
        return Err(
            ParseError("invalid_cursor", f"cursor {after.epoch}:{after.offset} is not in this feed")
        )
    return Ok(team_events(sq, team, after, follow=True))


def team_cursor(header: str | None, query: str | None) -> Ok[TeamCursor | None] | Err[ParseError]:
    """`Last-Event-ID` wins over `?after` (lane 25's split, fixed here), in both hosts."""
    raw = header or query
    if not raw:
        return Ok(None)
    epoch, sep, offset = raw.partition(":")
    if not sep or not all(p.lstrip("-").isdigit() for p in (epoch, offset)):
        return Err(ParseError("invalid_cursor", f"{raw} is not <epoch>:<offset>"))
    return Ok(TeamCursor(int(epoch), int(offset)))


def item_json(item: TeamItem) -> JsonValue:
    """One host-api TeamItem (host-api.v1.schema.json), snake_case, as the SSE `data`."""
    cursor: JsonValue = {"epoch": item.cursor.epoch, "offset": item.cursor.offset}
    if isinstance(item, EpochRestarted):
        return {"kind": "epoch_restarted", "cursor": cursor}
    return {
        "kind": "event",
        "cursor": cursor,
        "source": _source_json(item),
        "event": to_json(item.event),
    }


def _source_json(item: TeamEvent) -> JsonValue:
    match item.source:
        case MemberSource(member=member):
            return {"kind": "member", "member": to_json(member)}
        case OperatorSource(principal=principal, request=request):
            return {
                "kind": "operator",
                "principal": to_json(principal),
                "request": request,
            }
        case TeamLogSource():
            return {"kind": "team"}
        case _:
            assert_never(item.source)
