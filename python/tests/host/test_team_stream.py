"""GET /v1/teams/{team}/events (lane 29B): a lead team's feed as SSE. The route always follows,
closes on epoch_restarted, and answers 401, 404 and 400 the same way as the TypeScript host.
Mirrors TypeScript's host/test/team-stream.test.ts.

The refusals go through the whole ASGI app over httpx; the following stream is read from the
route's own body_iterator, because httpx's ASGITransport drains a response before returning it
and so cannot read a stream that stays open.
"""

import asyncio
import json
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import httpx
from host.test_http import as_, bearer, run, start, text, use
from starlette.responses import StreamingResponse

from threads import Agent, Store, agent, scripted_model, sqlite
from threads.agents.store import open_store, scoped
from threads.agents.team_handle_types import EpochRestarted, TeamCursor, TeamItem
from threads.host import Host, host
from threads.host.http import teams
from threads.result import Ok
from threads.team.rebuild import rebuild_team_index

if TYPE_CHECKING:
    from pydantic import JsonValue
    from starlette.requests import Request

OK = 200
BAD_REQUEST = 400
UNAUTHENTICATED = 401
NOT_FOUND = 404
WANTED_BEATS = 2
"""How many keepalives the quiet-stream test waits for before it lets its one item through."""


def _lead() -> Agent[None, str]:
    researcher = agent(name="researcher", model=scripted_model({"responses": [text("Fell.")]}))
    script: list[JsonValue] = [
        use("start", {"agent": "researcher", "task": "Go."}),
        text("Started."),
        text("Prices fell."),
    ]
    return agent(name="lead", model=scripted_model({"responses": script}), team=[researcher])


@asynccontextmanager
async def _served(
    bot: Agent[None, str], store: Store
) -> AsyncGenerator[tuple[Host, httpx.AsyncClient]]:
    served = host(store=store, agents={"support": bot}, authenticate=bearer)
    async with served:
        transport = httpx.ASGITransport(app=served.asgi)
        async with httpx.AsyncClient(transport=transport, base_url="http://host") as client:
            yield served, client


async def _ran(client: httpx.AsyncClient, store: Store) -> str:
    """The lead's run over the API, and its team's id."""
    body: JsonValue = {"agent": "support", "input": "Research prices."}
    assert (await start(client, "alice", "k-1", body)).status_code == 202  # noqa: PLR2004 - HTTP
    sq = await open_store(scoped(store, "acme"))
    rows = await sq.run(lambda c: c.execute("SELECT team_id FROM teams").fetchall())
    assert len(rows) == 1
    return str(rows[0][0])


def _request(
    team: str, who: str, *, query: str = "", last_event_id: str | None = None
) -> "Request":
    from starlette.requests import Request  # noqa: PLC0415 - starlette only in this test

    headers = [(b"authorization", f"Bearer {who}".encode())]
    if last_event_id is not None:
        headers.append((b"last-event-id", last_event_id.encode()))
    path = f"/v1/teams/{team}/events"
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "raw_path": path.encode(),
            "headers": headers,
            "query_string": query.encode(),
            "path_params": {"team": team},
        }
    )


async def _following(served: Host, request: "Request") -> StreamingResponse:
    response = await teams.subscribe(served)(request)
    assert isinstance(response, StreamingResponse)
    assert response.status_code == OK
    assert response.media_type == "text/event-stream"
    return response


def _text(chunk: str | bytes | memoryview) -> str:
    return chunk if isinstance(chunk, str) else bytes(chunk).decode()


async def _blocks(served: Host, request: "Request", n: int) -> list[str]:
    """`n` SSE blocks from a following response, then the reader lets go."""
    response = await _following(served, request)
    found: list[str] = []
    buffer = ""
    async for chunk in response.body_iterator:
        buffer += _text(chunk)
        parts = buffer.split("\n\n")
        buffer = parts.pop()
        found += parts
        if len(found) >= n:
            break
    return found[:n]


def _id(block: str) -> str:
    return block.split("\n", 1)[0].removeprefix("id: ")


def _data(block: str) -> "JsonValue":
    return json.loads(block[block.index("data: ") + 6 :])


def test_each_item_is_one_message_id_epoch_offset_and_canonical_json() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        async with _served(_lead(), store) as (served, client):
            team = await _ran(client, store)
            blocks = await _blocks(served, _request(team, "alice"), 2)
        assert [_id(b) for b in blocks] == ["1:1", "1:2"]
        # RFC 8785: keys in code-point order, and no spaces.
        assert blocks[0].startswith('id: 1:1\ndata: {"cursor":{"epoch":1,"offset":1},')
        first = _data(blocks[0])
        assert isinstance(first, dict)
        assert first["kind"] == "event"
        assert first["source"] == {"kind": "team"}

    run(main)


def test_an_unauthenticated_read_is_401_and_another_tenants_team_is_404() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        async with _served(_lead(), store) as (_served_host, client):
            team = await _ran(client, store)
            path = f"/v1/teams/{team}/events"
            assert (await client.get(path)).status_code == UNAUTHENTICATED
            # Its tenant is acme: eve's tenant has no such team.
            assert (await client.get(path, headers=as_("eve"))).status_code == NOT_FOUND

    run(main)


def test_the_host_teams_id_is_not_found_it_has_no_http_stream() -> None:
    async def main() -> None:
        # A tenant whose only team is a host team (Phase 2, lane 29D): reachable only through
        # Host.team, never over HTTP. Nothing runs here, so nothing else reads the row.
        store = sqlite(":memory:")
        team = "01a0c000-0000-7000-8000-0000000000aa"
        sq = await open_store(scoped(store, "acme"))
        await sq.run(
            lambda c: c.execute(
                "INSERT INTO teams"
                " (team_id, tenant_id, kind, lead_thread_id, team_log_branch_id, closed_at)"
                " VALUES (?, 'acme', 'host', NULL, ?, NULL)",
                (team, "01a0c000-0000-7000-8000-0000000000ab"),
            )
        )
        bot = agent(name="lead", model=scripted_model({"responses": [text("Ready.")]}))
        async with _served(bot, store) as (_served_host, client):
            found = await client.get(f"/v1/teams/{team}/events", headers=as_("alice"))
            assert found.status_code == NOT_FOUND
            assert found.json()["error"]["code"] == "not_found"
            # An id no team has is the same answer, so the host team is not distinguishable.
            unknown = await client.get(
                "/v1/teams/01a0c000-0000-7000-8000-0000000000ff/events", headers=as_("alice")
            )
            assert unknown.status_code == NOT_FOUND

    run(main)


def test_a_malformed_or_future_cursor_is_400_invalid_cursor() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        async with _served(_lead(), store) as (_served_host, client):
            team = await _ran(client, store)
            path = f"/v1/teams/{team}/events"
            # A later epoch than the feed's is refused, not restarted.
            for after in ("nonsense", "1:x", "1:2:3", "1:", " 1:2", "9:0"):
                bad = await client.get(path, params={"after": after}, headers=as_("alice"))
                assert bad.status_code == BAD_REQUEST, after
                assert bad.json()["error"]["code"] == "invalid_cursor"
            # A cursor past the head of this epoch is refused too.
            past = await client.get(path, params={"after": "1:9999"}, headers=as_("alice"))
            assert past.status_code == BAD_REQUEST

    run(main)


def test_last_event_id_wins_over_after() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        async with _served(_lead(), store) as (served, client):
            team = await _ran(client, store)
            # A good header beats a bad query.
            good = await _blocks(
                served, _request(team, "alice", query="after=nonsense", last_event_id="1:2"), 1
            )
            assert _id(good[0]) == "1:3"
            # A bad header beats a good query.
            beaten = await client.get(
                f"/v1/teams/{team}/events",
                params={"after": "1:2"},
                headers=as_("alice") | {"last-event-id": "9:0"},
            )
            assert beaten.status_code == BAD_REQUEST

    run(main)


def test_an_index_wipe_mid_follow_sends_one_epoch_restarted_and_closes() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        async with _served(_lead(), store) as (served, client):
            team = await _ran(client, store)
            sq = await open_store(scoped(store, "acme"))
            row = await sq.run(lambda c: c.execute("SELECT COUNT(*) FROM team_feed").fetchone())
            assert row is not None
            committed = int(str(row[0]))
            response = await _following(served, _request(team, "alice"))
            read = ""
            wiped = False
            async for chunk in response.body_iterator:
                read += _text(chunk)
                if not wiped and read.count("\n\n") >= committed:
                    wiped = True
                    assert isinstance(await rebuild_team_index(sq, team), Ok)
            messages = [b for b in read.split("\n\n") if "data: " in b]
            assert _data(messages[-1]) == {
                "kind": "epoch_restarted",
                "cursor": {"epoch": 2, "offset": 0},
            }
            assert _id(messages[-1]) == "2:0"
            assert len([m for m in messages if "epoch_restarted" in m]) == 1
            # The client reconnects with the new cursor and resumes in the new epoch.
            again = await _blocks(served, _request(team, "alice", last_event_id="2:0"), 1)
            assert _id(again[0]) == "2:1"

    run(main)


def test_a_quiet_stream_sends_keepalive_comments_which_move_no_cursor() -> None:
    async def main() -> None:
        # The one item waits until the test has seen two beats, so no clock decides the outcome.
        release = asyncio.Event()

        async def quiet() -> AsyncIterator[TeamItem]:
            await release.wait()
            yield EpochRestarted(TeamCursor(1, 0))

        beats = 0
        last = ""
        async for block in teams.sse_frames(quiet(), 0.001):
            last = block
            if block == ": keepalive\n\n":
                beats += 1
                if beats == WANTED_BEATS:
                    release.set()
                continue
            break
        assert beats == WANTED_BEATS
        # The beats moved no cursor: the item after them is still the first one.
        assert last == (
            'id: 1:0\ndata: {"cursor":{"epoch":1,"offset":0},"kind":"epoch_restarted"}\n\n'
        )

    run(main)
