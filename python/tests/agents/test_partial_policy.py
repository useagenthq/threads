"""agent(permissions=, retry=, context=) take only the fields you change, as in TypeScript: the
rest keep their defaults (spec/api.json agent, a partial of each Policy section)."""

import pytest

from threads import ConfigError, agent, scripted_model
from threads.agents.bindings import DEFAULT_PERMISSIONS
from threads.loop.defaults import CONTEXT, RETRY


def test_a_partial_section_is_merged_over_the_defaults() -> None:
    bot = agent(
        model=scripted_model({"responses": []}),
        permissions={"mode": "accept_edits", "allow": ["bash(ls:*)"]},
        retry={"max_retries": 2},
        context={"reserve_tokens": 1000},
    )
    d = bot.definition
    assert d.permissions == DEFAULT_PERMISSIONS.model_copy(
        update={"mode": "accept_edits", "allow": ["bash(ls:*)"]}
    )
    assert d.retry == RETRY.model_copy(update={"max_retries": 2})
    assert d.context == CONTEXT.model_copy(update={"reserve_tokens": 1000})


def test_a_complete_model_is_taken_as_is_and_a_bad_field_is_a_config_error() -> None:
    bot = agent(model=scripted_model({"responses": []}), retry=RETRY)
    assert bot.definition.retry == RETRY
    with pytest.raises(ConfigError) as refused:
        agent(model=scripted_model({"responses": []}), retry={"max_retries": "8"})
    assert refused.value.code == "invalid_config"
    with pytest.raises(ConfigError):
        agent(model=scripted_model({"responses": []}), permissions={"moed": "default"})
