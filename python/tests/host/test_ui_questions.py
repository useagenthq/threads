"""A question (lane 14A's ask_user) through both UI protocols, on the scripted model: the AI SDK
answers it with the tool's output (what addToolOutput sends), AG-UI with a resume whose payload
is an Answer, and the run goes on with the answer."""

from http import HTTPStatus

from host.test_http import run, served, text, use
from host.ui_kit import AG_UI, AI_SDK, chat, frames, post, types, user
from pydantic import JsonValue

from threads import Agent, agent, scripted_model

ASK: JsonValue = {"question": "Which colour?", "options": ["Red", "Blue"]}


def asker() -> Agent[None, str]:
    script: JsonValue = {"responses": [use("ask_user", ASK), text("Blue it is.")]}
    return agent(model=scripted_model(script))


def test_ai_sdk_answers_a_question_with_the_tools_output() -> None:
    async def main() -> None:
        async with served(asker()) as client:
            first = await post(client, AI_SDK, "alice", chat("c", user("m1", "Paint it")))
            assert "tool-input-available" in types(first)
            part: JsonValue = {
                "type": "tool-ask_user",
                "toolCallId": "call_1",
                "state": "output-available",
                "output": "Blue",
            }
            answered: JsonValue = {"id": "a1", "role": "assistant", "parts": [part]}
            again = chat("c", user("m1", "Paint it"), answered)
            second = await post(client, AI_SDK, "alice", again)
            assert second.status_code == HTTPStatus.OK
            deltas = [
                str(d["delta"])
                for _, d in frames(second)
                if isinstance(d, dict) and d.get("type") == "text-delta"
            ]
            assert "".join(deltas) == "Blue it is."

    run(main)


def test_ag_ui_answers_a_question_with_a_resume() -> None:
    async def main() -> None:
        async with served(asker()) as client:
            body: dict[str, JsonValue] = {
                "threadId": "chat-1",
                "runId": "r1",
                "messages": [{"id": "m1", "role": "user", "content": "Paint it"}],
                "tools": [],
            }
            last = frames(await post(client, AG_UI, "alice", body))[-1][1]
            assert isinstance(last, dict)
            outcome = last["outcome"]
            assert isinstance(outcome, dict)
            interrupts = outcome["interrupts"]
            assert isinstance(interrupts, list)
            asked = interrupts[0]
            assert isinstance(asked, dict)
            assert asked["reason"] == "user_input"
            resume: JsonValue = [
                {"interruptId": asked["id"], "status": "resolved", "payload": {"answer": "Blue"}}
            ]
            second = await post(client, AG_UI, "alice", {**body, "runId": "r2", "resume": resume})
            assert types(second)[-1] == "RUN_FINISHED"
            assert "Blue it is." in second.text

    run(main)
