"""Structured output through agent().run (ADR 0012 5a): final_output is checked strictly against
the output model, a plain-text end is asked again, max_retries failed candidates fail the run,
and a structured subagent reports the canonical JSON of its output."""

import asyncio
from collections.abc import Sequence
from datetime import date, datetime
from typing import Annotated, Literal
from uuid import UUID

import pytest
from pydantic import AnyUrl, BaseModel, Field, JsonValue
from pydantic.experimental.missing_sentinel import MISSING

from threads import Completed, ConfigError, EventItem, Failed, Thread, agent, scripted_model, sqlite
from threads.agents.store import open_store
from threads.log import (
    AgentFinishedEvent,
    Event,
    InjectedEvent,
    OutputValidatedEvent,
    ToolResultEvent,
)
from threads.log.digest import canonical_sha256
from threads.result import Ok

USAGE: JsonValue = {"input_tokens": 10, "output_tokens": 2}


class Verdict(BaseModel):
    fixed: bool
    tests: int


def text(reply: str) -> JsonValue:
    return {"content": [{"type": "text", "text": reply}], "stop_reason": "end_turn", "usage": USAGE}


def call(name: str, args: JsonValue, call_id: str) -> JsonValue:
    part: JsonValue = {"type": "tool_use", "call_id": call_id, "name": name, "input": args}
    return {"content": [part], "stop_reason": "tool_use", "usage": USAGE}


def final(args: JsonValue, call_id: str = "call_1") -> JsonValue:
    return call("final_output", args, call_id)


GOOD: JsonValue = {"fixed": True, "tests": 12}


async def events_of(thread: Thread) -> list[Event]:
    timeline = await thread.timeline()
    assert isinstance(timeline, Ok)
    return [e.event for e in timeline.value.entries]


def only[T](events: Sequence[Event], kind: type[T]) -> list[T]:
    return [e for e in events if isinstance(e, kind)]


def test_a_rejected_candidate_is_retried_and_the_accepted_one_is_the_typed_output() -> None:
    async def main() -> None:
        # "1" is not an int: validation is strict, never coerced.
        script: JsonValue = {"responses": [final({"fixed": True, "tests": "1"}), final(GOOD, "c2")]}
        bot = agent(model=scripted_model(script), output=Verdict)
        result = await bot.run("Is the bug fixed?", store=sqlite(":memory:"))
        assert isinstance(result, Completed)
        verdict: Verdict = result.output
        assert verdict == Verdict(fixed=True, tests=12)
        events = await events_of(result.thread)
        assert [v.data.outcome for v in only(events, OutputValidatedEvent)] == [
            "rejected",
            "accepted",
        ]
        rejected, accepted = (r.data.preview for r in only(events, ToolResultEvent))
        assert rejected.startswith("final_output rejected: ")
        assert accepted == '{"fixed":true,"tests":12}'

    asyncio.run(main())


def test_the_output_schema_is_pinned_and_final_output_is_the_last_tool() -> None:
    bot = agent(model=scripted_model({"responses": []}), output=Verdict, output_retries=3)
    pinned = bot.definition.thread_started()
    policy = pinned["policy"]
    assert isinstance(policy, dict)
    schema = Verdict.model_json_schema()
    digest = canonical_sha256(schema)
    assert isinstance(digest, Ok)
    assert policy["output"] == {
        "schema": schema,
        "schema_sha256": digest.value,
        "mode": "tool",
        "max_retries": 3,
    }
    tools = pinned["tools"]
    assert isinstance(tools, list)
    last = tools[-1]
    assert isinstance(last, dict)
    assert (last["name"], last["input_schema"], last["ends_turn"]) == ("final_output", schema, True)


def test_a_plain_text_end_is_asked_for_final_output() -> None:
    async def main() -> None:
        script: JsonValue = {"responses": [text("Yes, it is fixed."), final(GOOD)]}
        bot = agent(model=scripted_model(script), output=Verdict)
        result = await bot.run("Is the bug fixed?", store=sqlite(":memory:"))
        assert isinstance(result, Completed)
        assert result.output == Verdict(fixed=True, tests=12)
        asks = only(await events_of(result.thread), InjectedEvent)
        assert [(a.data.origin.id, a.data.trust) for a in asks] == [
            ("final_output", "trusted_instruction")
        ]

    asyncio.run(main())


def test_max_retries_failed_candidates_fail_the_run_output_invalid() -> None:
    async def main() -> None:
        # One rejected candidate and one plain-text end: two failed candidates.
        script: JsonValue = {"responses": [final({"fixed": "yes"}), text("It is fixed.")]}
        bot = agent(model=scripted_model(script), output=Verdict)
        result = await bot.run("Is the bug fixed?", store=sqlite(":memory:"))
        assert isinstance(result, Failed)
        assert result.error.code == "output_invalid"

    asyncio.run(main())


def test_zero_retries_fail_on_the_first_failed_candidate() -> None:
    async def main() -> None:
        script: JsonValue = {"responses": [final({"fixed": "yes"})]}
        bot = agent(model=scripted_model(script), output=Verdict, output_retries=0)
        result = await bot.run("Is the bug fixed?", store=sqlite(":memory:"))
        assert isinstance(result, Failed)
        assert result.error.code == "output_invalid"

    asyncio.run(main())


def test_stream_returns_the_typed_output() -> None:
    async def main() -> None:
        bot = agent(model=scripted_model({"responses": [final(GOOD)]}), output=Verdict)
        stream = bot.stream("Is the bug fixed?", store=sqlite(":memory:"))
        kinds = [i.event.type async for i in stream if isinstance(i, EventItem)]
        result = await stream.result
        assert isinstance(result, Completed)
        assert result.output == Verdict(fixed=True, tests=12)
        assert "output_validated" in kinds

    asyncio.run(main())


@pytest.mark.parametrize("output", [dict, Verdict(fixed=True, tests=1), "Verdict"])
def test_an_output_that_is_not_a_pydantic_model_class_is_refused(output: object) -> None:
    with pytest.raises(ConfigError) as raised:
        agent(model=scripted_model({"responses": []}), output=output)  # pyright: ignore[reportArgumentType] - the invalid input under test
    assert raised.value.code == "invalid_config"
    assert "output" in str(raised.value)


@pytest.mark.parametrize("retries", [True, -1, 1.5])
def test_output_retries_must_be_a_non_negative_integer(retries: object) -> None:
    with pytest.raises(ConfigError) as raised:
        agent(model=scripted_model({"responses": []}), output=Verdict, output_retries=retries)  # pyright: ignore[reportArgumentType] - the invalid input under test
    assert raised.value.code == "invalid_config"
    assert "output_retries" in str(raised.value)


def test_a_structured_subagent_reports_the_canonical_json_of_its_output() -> None:
    async def main() -> None:
        checker = agent(
            name="checker", model=scripted_model({"responses": [final(GOOD)]}), output=Verdict
        )
        spawn = call("spawn_agent", {"agent": "checker", "prompt": "Is it fixed?"}, "call_1")
        lead = agent(
            model=scripted_model({"responses": [spawn, text("It is fixed.")]}),
            subagents=[checker],
        )
        store = sqlite(":memory:")
        result = await lead.run("Ask the checker.", store=store)
        assert isinstance(result, Completed)
        events = await events_of(result.thread)
        shown = '{"fixed":true,"tests":12}'
        assert only(events, ToolResultEvent)[0].data.preview == shown
        (finished,) = only(events, AgentFinishedEvent)
        ref = finished.data.output_ref
        assert ref is not MISSING
        stored = await (await open_store(store)).get_artifact(ref.sha256)
        assert stored == Ok(shown.encode())

    asyncio.run(main())


class Line(BaseModel):
    sku: str
    qty: int


class Invoice(BaseModel):
    lines: list[Line]
    note: str | None = None


def test_a_nested_output_model_is_checked_and_typed() -> None:
    async def main() -> None:
        good: JsonValue = {"lines": [{"sku": "A-1", "qty": 2}]}
        script: JsonValue = {"responses": [final({"lines": [{"sku": "A-1"}]}), final(good, "c2")]}
        bot = agent(model=scripted_model(script), output=Invoice)
        result = await bot.run("Invoice the order.", store=sqlite(":memory:"))
        assert isinstance(result, Completed)
        assert result.output == Invoice(lines=[Line(sku="A-1", qty=2)])

    asyncio.run(main())


class Part(BaseModel):
    code: str = Field(min_length=2, max_length=4, pattern=r"^[A-Z]+$")
    size: Literal["s", "m"]
    parts: list["Part"] = Field(default_factory=list["Part"], max_length=2)


class Estimate(BaseModel):
    hours: int = Field(ge=1, le=40)
    root: Part


def test_constraints_enums_and_recursive_models_are_checked_never_crash_the_run() -> None:
    async def main() -> None:
        part: JsonValue = {"code": "AB", "size": "s", "parts": [{"code": "CD", "size": "m"}]}
        bad: JsonValue = {"hours": 41, "root": part}
        good: JsonValue = {"hours": 8, "root": part}
        script: JsonValue = {"responses": [final(bad), final(good, "c2")]}
        bot = agent(model=scripted_model(script), output=Estimate)
        result = await bot.run("Estimate it.", store=sqlite(":memory:"))
        assert isinstance(result, Completed)
        assert result.output.root.parts[0].code == "CD"

    asyncio.run(main())


class Meeting(BaseModel):
    at: datetime
    day: date
    id: UUID
    host: Annotated[str, Field(json_schema_extra={"format": "email"})]
    link: AnyUrl


def test_formats_are_checked_by_the_log_and_a_value_it_refuses_is_retried() -> None:
    async def main() -> None:
        good: dict[str, JsonValue] = {
            "at": "2026-09-23T10:00:00Z",
            "day": "2026-09-23",
            "id": "123e4567-e89b-12d3-a456-426614174000",
            "host": "ops@example.com",
            "link": "https://example.com/standup",
        }
        # Pydantic takes a naive datetime; the pinned schema's RFC 3339 date-time doesn't.
        naive = {**good, "at": "2026-09-23T10:00:00"}
        script: JsonValue = {"responses": [final(naive), final(good, "c2")]}
        bot = agent(model=scripted_model(script), output=Meeting)
        result = await bot.run("Book the standup.", store=sqlite(":memory:"))
        assert isinstance(result, Completed)
        assert str(result.output.id) == "123e4567-e89b-12d3-a456-426614174000"
        events = await events_of(result.thread)
        rejected = only(events, ToolResultEvent)[0].data.preview
        assert "fails the pinned output schema" in rejected

    asyncio.run(main())


class Pair(BaseModel):
    pair: tuple[int, str]


def test_an_output_model_the_log_cant_check_is_refused_at_setup() -> None:
    with pytest.raises(ConfigError) as raised:
        agent(model=scripted_model({"responses": []}), output=Pair)
    assert raised.value.code == "invalid_config"
    assert "Pair" in str(raised.value)
    assert "'prefixItems'" in str(raised.value)
