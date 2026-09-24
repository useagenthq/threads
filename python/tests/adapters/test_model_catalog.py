"""Lane 06: a model factory's limits come from the provider's exact-id catalog
(spec/models/<provider>.v1.json), per-field options win, the default output cap is
min(8192, max_output_tokens), and the cap is pinned as params.max_tokens and sent as exactly
that under the provider's own field name."""

import asyncio
import json
import pathlib
from collections.abc import Callable

import httpx2
import pytest
from fakes import FakeContext, Script, collect, line, sse
from pydantic import JsonValue

from threads._generated.model_catalogs import MODEL_CATALOGS
from threads.adapters.models import catalog as catalog_module
from threads.adapters.models.anthropic.model import AnthropicModel
from threads.adapters.models.catalog import ModelCatalog, catalog_for, parse_catalog
from threads.adapters.models.openai.model import OpenAIModel
from threads.adapters.models.options import Limits, resolve_limits
from threads.agents.config import ConfigError
from threads.anthropic import anthropic
from threads.litellm import litellm
from threads.openai import openai
from threads.result import Ok

VECTORS = (
    pathlib.Path(__file__).resolve().parents[3] / "spec/conformance/vectors/model-catalog.json"
)
CASES = json.loads(VECTORS.read_text())["cases"]
ACME = ModelCatalog.model_validate(
    {
        "version": 1,
        "provider": "acme",
        "entries": [
            {
                "id": "m-1",
                "max_input_tokens": 1000,
                "max_output_tokens": 100_000,
                "source": "https://acme.test/m-1",
                "verified": "2026-09-24",
            },
            {
                "id": "m-small",
                "max_input_tokens": 1000,
                "max_output_tokens": 4096,
                "source": "https://acme.test/m-small",
                "verified": "2026-09-24",
            },
        ],
        "withdrawn": [
            {
                "id": "m-small",
                "reason": "the page was misread",
                "use": {"max_input_tokens": 900, "max_output_tokens": 2048},
            }
        ],
    }
)


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_catalog_vectors_agree_with_zod(case: dict[str, JsonValue]) -> None:
    assert isinstance(parse_catalog(json.dumps(case["catalog"])), Ok) is case["valid"]


def test_every_listed_id_resolves_with_no_options() -> None:
    catalogs = [parse_catalog(text) for text in MODEL_CATALOGS]
    assert [c.value.provider for c in catalogs if isinstance(c, Ok)] == ["anthropic", "openai"]
    for parsed in catalogs:
        assert isinstance(parsed, Ok)
        for entry in parsed.value.entries:
            limits = resolve_limits(parsed.value.provider, entry.id, {})
            assert (limits.max_input_tokens, limits.max_output_tokens) == (
                entry.max_input_tokens,
                entry.max_output_tokens,
            )
            assert limits.max_tokens == min(8192, entry.max_output_tokens)


def test_each_option_overrides_one_limit() -> None:
    assert resolve_limits("acme", "m-1", {"max_input_tokens": 500}, ACME) == Limits(
        500, 100_000, 8192
    )
    assert resolve_limits("acme", "m-1", {"max_output_tokens": 2000}, ACME) == Limits(
        1000, 2000, 2000
    )
    assert resolve_limits("acme", "m-1", {"max_tokens": 32_000}, ACME) == Limits(
        1000, 100_000, 32_000
    )


def test_an_unknown_id_names_the_missing_options_and_the_listed_ids() -> None:
    # The withdrawn m-small is not offered.
    with pytest.raises(ConfigError) as refused:
        resolve_limits("acme", "m-9", {}, ACME)
    assert str(refused.value).endswith(
        'acme: unknown model "m-9"; pass max_input_tokens and max_output_tokens, or use a listed '
        "id: m-1"
    )
    with pytest.raises(ConfigError, match=r'"m-9"; pass max_output_tokens, or use a listed id'):
        resolve_limits("acme", "m-9", {"max_input_tokens": 10}, ACME)
    with pytest.raises(ConfigError, match=r'litellm: no catalog lists "x"; pass max_input_tokens'):
        resolve_limits("litellm", "x", {})


def test_a_near_miss_id_sees_the_ids_it_may_have_meant() -> None:
    with pytest.raises(ConfigError) as refused:
        anthropic("claude-haiku-4-5")
    assert str(refused.value).endswith(
        "or use a listed id: claude-fable-5-1, claude-haiku-4-5-20251001, claude-opus-5-5, "
        "claude-sonnet-5"
    )


def test_none_is_absent_so_the_catalog_value_applies() -> None:
    options: dict[str, object] = {"max_input_tokens": None, "max_tokens": None}
    limits = resolve_limits("acme", "m-1", options, ACME)  # pyright: ignore[reportArgumentType] - None on purpose, as an untyped caller would
    assert limits == Limits(1000, 100_000, 8192)


def test_a_limit_of_the_wrong_type_is_invalid_config_before_any_arithmetic() -> None:
    with pytest.raises(ConfigError) as refused:
        resolve_limits("acme", "m-1", {"max_output_tokens": "128000"}, ACME)  # pyright: ignore[reportArgumentType] - a string on purpose
    assert refused.value.code == "invalid_config"
    assert str(refused.value).endswith(
        "max_output_tokens must be a positive whole number of tokens, not '128000'"
    )


def test_the_embedded_catalog_can_t_be_changed_at_run_time() -> None:
    found = catalog_for("anthropic")
    assert found is not None
    assert isinstance(found.entries, list)
    found.entries.clear()
    assert anthropic("claude-sonnet-5").info.params == {"max_tokens": 8192}
    with pytest.raises(TypeError):
        catalog_module._CATALOGS["acme"] = ACME  # pyright: ignore[reportPrivateUsage, reportIndexIssue] - proving it is read-only


def test_a_withdrawn_id_prints_its_reason_the_corrected_and_the_original_values() -> None:
    with pytest.raises(ConfigError) as refused:
        resolve_limits("acme", "m-small", {}, ACME)
    assert refused.value.code == "invalid_config"
    message = str(refused.value)
    assert "the page was misread" in message
    assert "max_input_tokens=900 and max_output_tokens=2048 are correct" in message
    assert (
        "max_input_tokens=1000 and max_output_tokens=4096 continue threads started with the old "
        "values" in message
    )
    kept = resolve_limits(
        "acme", "m-small", {"max_input_tokens": 1000, "max_output_tokens": 4096}, ACME
    )
    assert kept == Limits(1000, 4096, 4096)


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"max_tokens": 0}, "max_tokens must be a positive whole number of tokens, not 0"),
        ({"max_tokens": 1.5}, "max_tokens must be a positive whole number of tokens, not 1.5"),
        ({"max_tokens": True}, "max_tokens must be a positive whole number of tokens, not True"),
        ({"max_tokens": 100_001}, "max_tokens 100001 is above max_output_tokens 100000; lower it"),
        (
            {"max_input_tokens": -1},
            "max_input_tokens must be a positive whole number of tokens, not -1",
        ),
    ],
)
def test_a_bad_limit_is_invalid_config(options: dict[str, object], message: str) -> None:
    with pytest.raises(ConfigError) as refused:
        resolve_limits("acme", "m-1", options, ACME)  # pyright: ignore[reportArgumentType] - wrong types on purpose
    assert (refused.value.code, str(refused.value).endswith(message)) == ("invalid_config", True)


def test_anthropic_and_openai_need_only_the_model_id() -> None:
    claude = anthropic("claude-sonnet-5").info
    assert claude.params == {"max_tokens": 8192}
    assert (claude.limits.context_window, claude.limits.max_output_tokens) == (1_000_000, 128_000)
    gpt = openai("gpt-5.5").info
    assert gpt.params == {"max_tokens": 8192}
    assert (gpt.limits.context_window, gpt.limits.max_output_tokens) == (922_000, 128_000)
    with pytest.raises(
        ConfigError, match=r'anthropic: unknown model "claude-next"; pass max_input'
    ):
        anthropic("claude-next")


type Make = Callable[[dict[str, JsonValue]], object]
CAP_SETTERS: list[tuple[Make, str]] = [
    (lambda p: anthropic("claude-sonnet-5", params=p), "max_tokens"),
    (lambda p: openai("gpt-5.5", params=p), "max_tokens"),
    (lambda p: openai("gpt-5.5", params=p), "max_output_tokens"),
    (
        lambda p: litellm("openai/x", max_input_tokens=9, max_output_tokens=9, params=p),
        "max_tokens",
    ),
]


@pytest.mark.parametrize(
    ("make", "key"),
    CAP_SETTERS,
    ids=["anthropic-max_tokens", "openai-max_tokens", "openai-max_output_tokens", "litellm"],
)
def test_params_can_t_set_the_cap(make: Make, key: str) -> None:
    with pytest.raises(ConfigError, match=f"params can't set {key}: pass max_tokens"):
        make({key: 10})


def test_litellm_takes_the_default_cap() -> None:
    made = litellm("openai/x", max_input_tokens=100_000, max_output_tokens=32_000)
    assert made.info.params == {"max_tokens": 8192}


def _sent(send_model: AnthropicModel | OpenAIModel, script: Script) -> dict[str, JsonValue]:
    info = send_model.info
    head: JsonValue = {
        "adapter": info.adapter.model_dump(mode="json"),
        "model": info.model.model_dump(mode="json"),
        "params": dict(info.params),
        "system": "",
        "tools": [],
    }
    body = line(head) + line({"role": "user", "content": [{"type": "text", "text": "hi"}]})
    asyncio.run(collect(send_model.send, body, FakeContext()))
    sent = script.bodies()[0]
    assert isinstance(sent, dict)
    return sent


def test_anthropic_sends_the_pinned_cap_as_max_tokens() -> None:
    script = Script([sse([(None, {"type": "message_stop"})])])
    info = anthropic("claude-sonnet-5", max_tokens=2048).info
    sent = _sent(AnthropicModel(info, "sk-test-1", http=httpx2.MockTransport(script)), script)
    assert (info.params, sent["max_tokens"]) == ({"max_tokens": 2048}, 2048)


def test_openai_sends_the_pinned_cap_as_max_output_tokens() -> None:
    script = Script([sse([(None, {"type": "response.completed", "response": {}})])])
    info = openai("gpt-5.5", max_tokens=2048).info
    sent = _sent(OpenAIModel(info, "sk-test-1", http=httpx2.MockTransport(script)), script)
    assert (info.params, sent["max_output_tokens"]) == ({"max_tokens": 2048}, 2048)
    assert "max_tokens" not in sent
