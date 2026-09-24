"""ask_user over the HTTP API: a run started by an API call is offered ask_user and parks; the
answer route takes only the asker's option (409 invalid_answer otherwise, question still open),
then resumes the run."""

from http import HTTPStatus

from host.test_api_recovery import until
from host.test_http import as_, run, served, start, text, use
from pydantic import JsonValue

from threads import agent, scripted_model, sqlite
from threads.agents.store import open_store, scoped
from threads.log import ThreadId, ToolResultEvent, TurnCompletedEvent
from threads.result import Ok

COLOR: JsonValue = {"question": "Which color?", "options": ["red", "blue"]}


def test_the_answer_route_takes_only_an_option_from_the_asker() -> None:
    async def main() -> None:
        script: JsonValue = {"responses": [use("ask_user", COLOR), text("Blue it is.")]}
        store = sqlite(":memory:")
        async with served(agent(model=scripted_model(script)), store=store) as client:
            started = await start(client, "alice", "k1", {"agent": "support", "input": "Paint."})
            assert started.status_code == HTTPStatus.ACCEPTED
            thread = ThreadId(started.json()["thread_id"])
            sq = await open_store(scoped(store, "acme"))

            async def events() -> list[object]:
                root = await sq.root(thread)
                assert isinstance(root, Ok)
                read = await sq.read(root.value, 0)
                assert isinstance(read, Ok)
                return list(read.value.fold.events)

            async def parked() -> bool:
                root = await sq.root(thread)
                assert isinstance(root, Ok)
                folded = await sq.read(root.value, 0)
                return isinstance(folded, Ok) and bool(folded.value.fold.parked)

            await until(parked)
            path = f"/v1/threads/{thread}/questions/call_1/answer"
            wrong = await client.post(path, json={"answer": "green"}, headers=as_("alice"))
            assert (wrong.status_code, wrong.json()["error"]["code"]) == (409, "invalid_answer")
            other = await client.post(path, json={"answer": "2"}, headers=as_("bob"))
            assert other.json()["error"]["code"] == "forbidden"
            done = await client.post(path, json={"answer": "2"}, headers=as_("alice"))
            assert done.status_code == HTTPStatus.OK

            async def ended() -> bool:
                return any(isinstance(e, TurnCompletedEvent) for e in await events())

            await until(ended)
            (answer,) = [e for e in await events() if isinstance(e, ToolResultEvent)]
            assert answer.data.preview == "blue"

    run(main)
