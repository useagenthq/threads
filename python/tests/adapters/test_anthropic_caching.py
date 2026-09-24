"""Anthropic prompt caching (lane 10): the shared request vector compared as canonical JSON, cache
placement, the pin and declared lifetime, the refusals that keep one TTL per request, the derived
cache-write price, usage that can't be priced, and a cached run through the real loop."""

import asyncio
import base64
from collections.abc import Generator, Mapping, Sequence
from pathlib import Path
from typing import Literal, TypedDict, Unpack

import httpx2
import pytest
from fakes import FakeContext, Script, collect, line, sse
from pydantic import JsonValue, TypeAdapter
from pydantic.experimental.missing_sentinel import MISSING

from threads import Completed, ConfigError, agent, sqlite
from threads.adapters.loop_resources import holding
from threads.adapters.models.anthropic.model import AnthropicModel
from threads.adapters.models.anthropic.request import build
from threads.adapters.models.render import UnsupportedContentError, parse
from threads.anthropic import anthropic
from threads.log import Price
from threads.log.jcs import canonicalize
from threads.loop.guard import block_model_requests
from threads.loop.model import Done
from threads.result import Ok

VECTOR = Path(__file__).parents[3] / "spec" / "conformance" / "vectors" / "anthropic-requests.json"
_OBJECT: TypeAdapter[dict[str, JsonValue]] = TypeAdapter(dict[str, JsonValue])
SEARCH: dict[str, JsonValue] = {"type": "web_search_20250305", "name": "web_search"}


class Options(TypedDict, total=False):
    """The anthropic() options these tests vary; the limits are fixed."""

    params: Mapping[str, JsonValue]
    price: Price
    hosted_tools: Sequence[Mapping[str, JsonValue]]
    citations: bool
    prompt_cache: Literal["5m", "1h"] | Literal[False]


def claude(**options: Unpack[Options]) -> AnthropicModel:
    return anthropic("claude-sonnet-5", max_input_tokens=200_000, max_output_tokens=4096, **options)


def _vector() -> dict[str, JsonValue]:
    return _OBJECT.validate_json(VECTOR.read_bytes())


def _canonical(value: JsonValue) -> str:
    text = canonicalize(value)
    assert isinstance(text, Ok)
    return text.value


def _context() -> FakeContext:
    blobs = _vector()["artifacts"]
    assert isinstance(blobs, dict)
    named: dict[str, JsonValue] = {str(k): v for k, v in blobs.items()}
    return FakeContext({k: base64.b64decode(str(v)) for k, v in named.items()})


def _map(render: bytes) -> dict[str, JsonValue]:
    return asyncio.run(build(parse(render), _context())).json


def _cases() -> list[tuple[str, str, JsonValue]]:
    cases = _vector()["cases"]
    assert isinstance(cases, list)
    out: list[tuple[str, str, JsonValue]] = []
    for c in cases:
        assert isinstance(c, dict)
        out.append((str(c["name"]), str(c["render"]), c["body"]))
    return out


@pytest.mark.parametrize(("name", "render", "body"), _cases(), ids=[c[0] for c in _cases()])
def test_the_shared_vector_maps_to_the_same_canonical_json(
    name: str, render: str, body: JsonValue
) -> None:
    assert _canonical(_map(render.encode())) == _canonical(body), name


def _head(settings: dict[str, JsonValue]) -> JsonValue:
    return {
        "adapter": {"name": "anthropic", "version": "1", "settings": settings},
        "model": {"provider": "anthropic", "name": "claude-sonnet-5"},
        "params": {"max_tokens": 1024},
        "system": "",
        "tools": [],
    }


HI: JsonValue = {"role": "user", "content": [{"type": "text", "text": "hi"}]}


def test_no_system_and_no_tools_get_automatic_caching_only() -> None:
    body = _map(line(_head({"prompt_cache": "5m"})) + line(HI))
    assert body["cache_control"] == {"type": "ephemeral"}
    assert "system" not in body
    assert "tools" not in body


@pytest.mark.parametrize("ttl", ["10m", True])
def test_a_line_0_with_another_prompt_cache_is_refused_before_dispatch(ttl: JsonValue) -> None:
    with pytest.raises(UnsupportedContentError) as refused:
        _map(line(_head({"prompt_cache": ttl})) + line(HI))
    assert refused.value.code == "continuation_unsupported"


def test_the_default_pins_5m_and_declares_a_5_minute_lifetime() -> None:
    info = claude().info
    assert info.adapter.settings == {"prompt_cache": "5m"}
    assert info.cache == {"ttl_ms": 300_000}


def test_1h_pins_1h_and_false_pins_nothing() -> None:
    hour = claude(prompt_cache="1h").info
    assert (hour.adapter.settings, hour.cache) == ({"prompt_cache": "1h"}, {"ttl_ms": 3_600_000})
    off = claude(prompt_cache=False).info
    assert (off.adapter.settings, off.cache) == ({}, "none")


def test_citations_are_pinned_when_on() -> None:
    info = claude(citations=True).info
    assert info.adapter.settings == {"prompt_cache": "5m", "citations": True}


@pytest.mark.parametrize(
    ("where", "params", "hosted"),
    [
        ("params", {"cache_control": {"type": "ephemeral"}}, []),
        ("params", {"metadata": [{"nested": {"cache_control": {"ttl": "1h"}}}]}, []),
        ("hosted_tools", {}, [{**SEARCH, "cache_control": {"type": "ephemeral"}}]),
    ],
)
def test_cache_control_anywhere_is_refused(
    where: str, params: dict[str, JsonValue], hosted: list[dict[str, JsonValue]]
) -> None:
    with pytest.raises(ConfigError) as raised:
        claude(params=params, hosted_tools=hosted)
    assert raised.value.message == f"anthropic {where} can't set cache_control: pass prompt_cache"


def test_1h_with_a_hosted_tool_is_refused_naming_both_options() -> None:
    with pytest.raises(ConfigError) as raised:
        claude(prompt_cache="1h", hosted_tools=[SEARCH])
    assert raised.value.message == (
        "prompt_cache 1h can't be combined with hosted_tools: Anthropic caches their results "
        "for 5 minutes, so writes would be billed at two rates"
    )
    claude(hosted_tools=[SEARCH])


def test_a_prompt_cache_outside_the_option_type_is_refused() -> None:
    with pytest.raises(ConfigError) as raised:
        claude(prompt_cache="10m")  # type: ignore[arg-type] - an untyped caller
    assert raised.value.message == 'anthropic prompt_cache must be "5m", "1h" or False, not \'10m\''


@pytest.mark.parametrize(
    "params",
    [{"output_format": {"type": "json_schema"}}, {"output_config": {"format": {"type": "x"}}}],
)
def test_citations_with_native_structured_output_are_refused(
    params: dict[str, JsonValue],
) -> None:
    with pytest.raises(ConfigError) as raised:
        claude(citations=True, params=params)
    assert raised.value.code == "invalid_config"
    claude(citations=True, params={"output_config": {"effort": "high"}})


def _write_price(price: Price, ttl: Literal["5m", "1h"] | Literal[False]) -> object:
    limits = claude(price=price, prompt_cache=ttl).info.limits
    assert isinstance(limits.price, Price)
    return limits.price.cache_write


@pytest.mark.parametrize(
    ("price", "ttl", "cache_write"),
    [
        (Price(input=3000, output=15_000, cache_read=300), "5m", 3750),
        (Price(input=3000, output=15_000, cache_read=300), "1h", 6000),
        (Price(input=3, output=15), "5m", 4),  # rounded up to whole nano-units
        (Price(input=3000, output=15_000, cache_write=4000), "5m", 4000),  # explicit wins
        (Price(input=3000, output=15_000), False, MISSING),  # no caching derives nothing
    ],
)
def test_the_cache_write_price_follows_the_pinned_ttl(
    price: Price, ttl: Literal["5m", "1h"] | Literal[False], cache_write: object
) -> None:
    assert _write_price(price, ttl) == cache_write


def _reply(usage: dict[str, JsonValue]) -> httpx2.Response:
    events: list[tuple[str, dict[str, JsonValue]]] = [
        ("message_start", {"message": {"id": "m", "usage": usage}}),
        ("content_block_start", {"index": 0, "content_block": {"type": "text", "text": "ok"}}),
        ("content_block_stop", {"index": 0}),
        ("message_delta", {"delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}}),
        ("message_stop", {}),
    ]
    return sse([(name, {"type": name, **data}) for name, data in events])


def _split(five: int, hour: int) -> dict[str, JsonValue]:
    return {
        "input_tokens": 5,
        "cache_creation_input_tokens": five + hour,
        "cache_creation": {"ephemeral_5m_input_tokens": five, "ephemeral_1h_input_tokens": hour},
    }


def _writes(pinned: str, usage: dict[str, JsonValue]) -> object:
    info = claude().info
    model = AnthropicModel(
        info, "sk-test-anthropic", http=httpx2.MockTransport(Script([_reply(usage)]))
    )
    body = line(_head({"prompt_cache": pinned})) + line(HI)
    chunks = asyncio.run(collect(model.send, body, FakeContext()))
    done = chunks[-1]
    assert isinstance(done, Done)
    return done.usage.cache_write_tokens


def test_writes_at_the_pinned_ttl_are_recorded() -> None:
    usage = _split(900, 0)
    assert _writes("5m", usage) == usage["cache_creation_input_tokens"]


def test_writes_under_the_other_ttl_are_unknown_never_mispriced() -> None:
    assert _writes("1h", _split(148, 100)) is None
    assert _writes("5m", _split(0, 50)) is None


@pytest.fixture
def offline() -> Generator[None]:
    """These adapters send only to a scripted transport."""
    block_model_requests(blocked=False)
    yield
    block_model_requests()


def _claude(script: Script, **options: Unpack[Options]) -> AnthropicModel:
    info = claude(**options).info
    return AnthropicModel(info, "sk-test-anthropic", http=httpx2.MockTransport(script))


PLAIN: dict[str, JsonValue] = {"input_tokens": 9}


@pytest.mark.usefixtures("offline")
def test_a_cached_run_keeps_line_0_byte_stable_and_prefixes_history() -> None:
    script = Script([_reply(PLAIN), _reply(PLAIN)])

    async def main() -> None:
        bot = agent(instructions="Answer briefly.", model=_claude(script))
        async with holding():
            first = await bot.run("hi", store=sqlite(":memory:"))
            second = await bot.run("again", thread=first.thread)
        assert isinstance(second, Completed)
        # replay re-renders every request and checks C7 per settings epoch.
        assert isinstance(await second.thread.replay(), Ok)

    asyncio.run(main())
    one, two = script.bodies()
    assert isinstance(one, dict)
    assert isinstance(two, dict)
    assert two["system"] == one["system"]
    assert two["cache_control"] == {"type": "ephemeral"}
    old, new = one["messages"], two["messages"]
    assert isinstance(old, list)
    assert isinstance(new, list)
    assert new[: len(old)] == old


@pytest.mark.usefixtures("offline")
def test_a_thread_started_before_caching_continues_only_with_prompt_cache_false() -> None:
    later = Script([_reply(PLAIN)])

    async def main() -> None:
        off = agent(model=_claude(Script([_reply(PLAIN)]), prompt_cache=False))
        async with holding():
            first = await off.run("hi", store=sqlite(":memory:"))
            with pytest.raises(ConfigError, match="another config"):
                await agent(model=_claude(Script([]))).run("again", thread=first.thread)
            again = agent(model=_claude(later, prompt_cache=False))
            assert isinstance(await again.run("again", thread=first.thread), Completed)

    asyncio.run(main())
    (body,) = later.bodies()
    assert isinstance(body, dict)
    assert "cache_control" not in body


@pytest.mark.usefixtures("offline")
def test_mixed_ttl_writes_leave_the_attempt_cost_incomplete() -> None:
    price = Price(input=3000, output=15_000, cache_read=300)
    script = Script([_reply(_split(148, 100))])

    async def main() -> None:
        bot = agent(model=_claude(script, prompt_cache="1h", price=price))
        async with holding():
            done = await bot.run("hi", store=sqlite(":memory:"))
        cost = await done.thread.cost()
        assert isinstance(cost, Ok)
        assert cost.value is not None
        assert cost.value.complete is False

    asyncio.run(main())
