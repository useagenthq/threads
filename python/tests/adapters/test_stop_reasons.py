"""Adapter to loop: each provider's stop signal maps to the stop reason whose turn ending the
spec table pins (spec/schema/README.md, "Turn endings by stop_reason"). The loop runs for real
over a scripted HTTP transport, so no request leaves the process."""

import asyncio
from collections.abc import Generator, Sequence

import httpx2
import pytest
from corpus import Clock
from fakes import Script, sse
from kit import T0, Tools, open_store, start
from pydantic import JsonValue

from threads.adapters.models.anthropic.model import AnthropicModel
from threads.adapters.models.litellm.model import LiteLLMModel
from threads.adapters.models.openai.model import OpenAIModel
from threads.anthropic import anthropic
from threads.litellm import litellm
from threads.log import ModelResponseEvent
from threads.loop.drive import drive
from threads.loop.guard import block_model_requests
from threads.loop.model import Model
from threads.loop.runtime import Idle, Runtime
from threads.openai import openai


@pytest.fixture(autouse=True)
def offline_adapters() -> Generator[None]:
    """The guard stops real providers; these adapters send only to a scripted transport."""
    block_model_requests(blocked=False)
    yield
    block_model_requests()


def drive_turn(model: Model) -> tuple[object, Runtime]:
    async def main() -> tuple[object, Runtime]:
        clock = Clock(T0)
        rt = await start(await open_store(), [], model, Tools({}, clock), clock)
        return await drive(rt), rt

    return asyncio.run(main())


def stops(rt: Runtime) -> list[str]:
    return [e.data.stop_reason for e in rt.events if isinstance(e, ModelResponseEvent)]


def claude(*replies: httpx2.Response) -> tuple[AnthropicModel, Script]:
    script = Script(list(replies))
    info = anthropic("claude-test", context_window=200_000, max_output_tokens=64).info
    return AnthropicModel(info, "sk-test-anthropic", http=httpx2.MockTransport(script)), script


def said(text: str, stop: str) -> httpx2.Response:
    events: Sequence[tuple[str, dict[str, JsonValue]]] = [
        ("message_start", {"message": {"usage": {"input_tokens": 1, "output_tokens": 1}}}),
        ("content_block_start", {"index": 0, "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"index": 0, "delta": {"type": "text_delta", "text": text}}),
        ("content_block_stop", {"index": 0}),
        ("message_delta", {"delta": {"stop_reason": stop}, "usage": {"output_tokens": 2}}),
        ("message_stop", {}),
    ]
    return sse([(name, {"type": name, **data}) for name, data in events])


def test_anthropic_pause_turn_asks_again_with_the_paused_content_as_is() -> None:
    model, script = claude(said("Searching.", "pause_turn"), said("Done.", "end_turn"))
    halt, rt = drive_turn(model)
    assert halt == Idle("end_turn")
    assert stops(rt) == ["pause_turn", "end_turn"]
    first, second = script.bodies()
    assert isinstance(first, dict)
    assert isinstance(second, dict)
    paused: JsonValue = {"role": "assistant", "content": [{"type": "text", "text": "Searching."}]}
    assert second["messages"] == [*_list(first["messages"]), paused]


def _list(value: JsonValue) -> list[JsonValue]:
    assert isinstance(value, list)
    return value


def test_anthropic_context_window_exceeded_ends_the_turn_context_exhausted() -> None:
    model, _ = claude(said("Partial", "model_context_window_exceeded"))
    halt, rt = drive_turn(model)
    assert stops(rt) == ["context_window_exceeded"]
    assert halt == Idle("context_exhausted")


def test_an_anthropic_stop_it_cannot_name_ends_the_turn_with_error() -> None:
    model, _ = claude(said("?", "some_future_reason"))
    halt, rt = drive_turn(model)
    assert stops(rt) == ["other"]
    assert halt == Idle("error")


def gpt(*replies: httpx2.Response) -> OpenAIModel:
    info = openai(
        "gpt-test", context_window=400_000, max_output_tokens=64, api_key="sk-test-1"
    ).info
    return OpenAIModel(info, "sk-test-openai", http=httpx2.MockTransport(Script(list(replies))))


def responded(kind: str, reason: str | None = None) -> httpx2.Response:
    content: JsonValue = [{"type": "output_text", "text": "Hi", "annotations": []}]
    message: JsonValue = {"type": "message", "role": "assistant", "content": content}
    response: dict[str, JsonValue] = {"usage": {"input_tokens": 1, "output_tokens": 1}}
    if reason is not None:
        response["incomplete_details"] = {"reason": reason}
    done: JsonValue = {"type": kind, "response": response}
    return sse([(None, {"type": "response.output_item.done", "item": message}), (None, done)])


@pytest.mark.parametrize(
    ("reason", "stop", "ending"),
    [("content_filter", "other", "error"), ("max_messages", "other", "error")],
)
def test_an_incomplete_openai_response_maps_its_reason(reason: str, stop: str, ending: str) -> None:
    halt, rt = drive_turn(gpt(responded("response.incomplete", reason)))
    assert stops(rt) == [stop]
    assert halt == Idle(ending)


def test_openai_max_output_tokens_is_max_tokens_and_the_loop_continues() -> None:
    model = gpt(
        responded("response.incomplete", "max_output_tokens"), responded("response.completed")
    )
    halt, rt = drive_turn(model)
    assert stops(rt) == ["max_tokens", "end_turn"]
    assert halt == Idle("end_turn")


def test_a_litellm_finish_reason_it_cannot_name_ends_the_turn_with_error() -> None:
    async def complete(**_kwargs: object) -> object:
        async def chunks() -> object:
            yield {"choices": [{"delta": {"content": "x"}, "finish_reason": "content_filter"}]}

        return chunks()

    info = litellm(
        "openai/gpt-test", context_window=1000, max_output_tokens=8, api_key="sk-test-1"
    ).info
    halt, rt = drive_turn(LiteLLMModel(info, "sk-test-litellm", lambda _key: complete))
    assert stops(rt) == ["other"]
    assert halt == Idle("error")
