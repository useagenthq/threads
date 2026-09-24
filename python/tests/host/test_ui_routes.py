"""The UI routes end to end on the scripted model (spec/schema/ui/README.md): a chat key names
the principal's thread, a message starts one run and its retry replays it, an approval resumes
the run, and every refusal happens before a stream starts, appending nothing."""

import sqlite3
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING

from host.test_http import ALICE, BOB, as_, run, sender, served
from host.ui_kit import AG_UI, AI_SDK, chat, frames, post, types, user

from threads import sqlite
from threads.host.ui.key import ui_thread_id

if TYPE_CHECKING:
    from pydantic import JsonValue


def test_a_message_streams_its_run_to_an_approval_then_the_approval_resumes_it() -> None:
    async def main() -> None:
        async with served(sender([])) as client:
            first = await post(client, AI_SDK, "alice", chat("chat-1", user("m1", "Send x")))
            assert first.status_code == HTTPStatus.OK
            assert first.headers["x-vercel-ai-ui-message-stream"] == "v1"
            assert types(first) == [
                "start",
                "start-step",
                "tool-input-available",
                "finish-step",
                "tool-approval-request",
                "finish",
                "[DONE]",
            ]
            request = next(
                d
                for _, d in frames(first)
                if isinstance(d, dict) and d.get("type") == "tool-approval-request"
            )
            assert isinstance(request, dict)
            approved: JsonValue = {
                "id": "assistant-1",
                "role": "assistant",
                "parts": [
                    {
                        "type": "tool-send",
                        "toolCallId": "call_1",
                        "state": "approval-responded",
                        "approval": {"id": request["approvalId"], "approved": True},
                    }
                ],
            }
            second = await post(
                client, AI_SDK, "alice", chat("chat-1", user("m1", "Send x"), approved)
            )
            assert types(second) == [
                "start",
                "tool-output-available",
                "start-step",
                "text-start",
                "text-delta",
                "text-end",
                "finish-step",
                "finish",
                "[DONE]",
            ]

    run(main)


def test_a_retried_message_replays_its_run_and_appends_nothing() -> None:
    async def main() -> None:
        async with served(sender([])) as client:
            body = chat("chat-1", user("m1", "Send x"))
            first = await post(client, AI_SDK, "alice", body)
            again = await post(client, AI_SDK, "alice", body)
            assert frames(first) == frames(again)
            other = await post(client, AI_SDK, "alice", chat("chat-1", user("m1", "Other")))
            assert other.status_code == HTTPStatus.CONFLICT
            assert other.json()["error"]["code"] == "idempotency_key_reused"

    run(main)


def test_refusals_come_before_any_stream() -> None:
    async def main() -> None:
        async with served(sender([])) as client:
            regen = chat("chat-1", user("m1", "x")) | {"trigger": "regenerate-message"}
            bad_key = chat("not a key", user("m1", "x"))
            file_part: JsonValue = {"id": "m1", "role": "user", "parts": [{"type": "file"}]}
            tools: dict[str, JsonValue] = {
                "threadId": "t1",
                "runId": "r1",
                "messages": [{"id": "m1", "role": "user", "content": "x"}],
                "tools": [{"name": "x"}],
            }
            refusals: list[tuple[str, dict[str, JsonValue]]] = [
                (AI_SDK, regen),
                (AI_SDK, bad_key),
                (AI_SDK, chat("chat-1", file_part)),
                (AG_UI, tools),
                (AI_SDK, {"messages": []}),
            ]
            for path, bad in refusals:
                answered = await post(client, path, "alice", bad)
                assert answered.status_code == HTTPStatus.BAD_REQUEST, bad
                assert answered.json()["error"]["code"] == "invalid_request"
            assert (
                await post(client, AI_SDK, "nobody", chat("c", user("m", "x")))
            ).status_code == HTTPStatus.UNAUTHORIZED
            missing = await client.post("/v1/ui/ai-sdk/nobody", json={}, headers=as_("alice"))
            assert missing.status_code == HTTPStatus.NOT_FOUND
            nothing = await post(
                client,
                AI_SDK,
                "alice",
                chat("chat-9", {"id": "a", "role": "assistant", "parts": []}),
            )
            assert nothing.status_code == HTTPStatus.NOT_FOUND

    run(main)


def test_the_same_key_from_two_principals_is_two_threads_and_the_key_is_never_stored(
    tmp_path: Path,
) -> None:
    assert ui_thread_id(ALICE, "support", "k") != ui_thread_id(BOB, "support", "k")
    path = str(tmp_path / "store")

    async def main() -> None:
        async with served(sender([]), store=sqlite(path)) as client:
            await post(client, AI_SDK, "alice", chat("secret-chat-key", user("m1", "Send x")))
            await post(client, AI_SDK, "bob", chat("secret-chat-key", user("m1", "Send x")))

    run(main)
    with sqlite3.connect(tmp_path / "store" / "threads.db") as conn:
        threads = conn.execute("SELECT count(*) FROM threads").fetchone()[0]
        dumped = "\n".join(conn.iterdump())
    assert threads == len((ALICE, BOB))
    assert "secret-chat-key" not in dumped


def test_ag_ui_interrupt_then_resume() -> None:
    async def main() -> None:
        async with served(sender([])) as client:
            body: dict[str, JsonValue] = {
                "threadId": "chat-1",
                "runId": "r1",
                "messages": [{"id": "m1", "role": "user", "content": "Send x"}],
                "tools": [],
            }
            first = await post(client, AG_UI, "alice", body)
            last = frames(first)[-1][1]
            assert isinstance(last, dict)
            outcome = last["outcome"]
            assert isinstance(outcome, dict)
            assert outcome["type"] == "interrupt"
            interrupts = outcome["interrupts"]
            assert isinstance(interrupts, list)
            assert isinstance(interrupts[0], dict)
            resumed: dict[str, JsonValue] = {
                **body,
                "runId": "r2",
                "resume": [
                    {
                        "interruptId": interrupts[0]["id"],
                        "status": "resolved",
                        "payload": {"decision": "grant"},
                    }
                ],
            }
            second = await post(client, AG_UI, "alice", resumed)
            kinds = types(second)
            assert kinds[:2] == ["RUN_STARTED", "MESSAGES_SNAPSHOT"]
            assert kinds[-1] == "RUN_FINISHED"
            assert "TOOL_CALL_RESULT" in kinds

    run(main)


def test_the_cursor_route() -> None:
    async def main() -> None:
        async with served(sender([])) as client:
            started = await client.post(
                "/v1/runs",
                json={"agent": "support", "input": "Send x"},
                headers=as_("alice") | {"idempotency-key": "k1"},
            )
            accepted = started.json()
            base = f"/v1/threads/{accepted['thread_id']}/runs/{accepted['run_id']}/ui"
            whole = await client.get(f"{base}/ai-sdk", headers=as_("alice"))
            ids = [i for i, _ in frames(whole) if i is not None]
            resumed = await client.get(f"{base}/ai-sdk?after={ids[1]}", headers=as_("alice"))
            later = [f for f in frames(whole) if f[0] is not None][2:]
            assert [f for f in frames(resumed) if f[0] is not None] == later
            bad = await client.get(f"{base}/ai-sdk?after=999:0", headers=as_("alice"))
            assert bad.status_code == HTTPStatus.BAD_REQUEST
            assert bad.json()["error"]["code"] == "invalid_cursor"
            unknown = await client.get(f"{base}/nope", headers=as_("alice"))
            assert unknown.status_code == HTTPStatus.NOT_FOUND
            ag = await client.get(f"{base}/ag-ui?after={ids[0]}", headers=as_("alice"))
            assert types(ag)[:2] == ["RUN_STARTED", "MESSAGES_SNAPSHOT"]

    run(main)
