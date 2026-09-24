"""ready() refuses a channel adapter without render_text: hosts offer ask_user on every channel
thread, so an adapter written before it would fail the first time a question is posted."""

import asyncio
from dataclasses import dataclass

import pytest
from host.test_channel_approvals import ItemsChannel

from threads import ConfigError, agent, scripted_model, sqlite
from threads.host import host


@dataclass
class Legacy(ItemsChannel):
    """An adapter from before render_text."""

    render_text: None = None  # pyright: ignore[reportIncompatibleMethodOverride] - the old shape


def test_ready_refuses_a_channel_adapter_without_render_text() -> None:
    bot = agent(model=scripted_model({"responses": []}))
    old = Legacy()
    served = host(store=sqlite(":memory:"), agents={"bot": bot}, channels={"old": old})  # pyright: ignore[reportArgumentType] - an adapter missing render_text
    with pytest.raises(ConfigError) as refused:
        asyncio.run(served.ready())
    assert refused.value.code == "invalid_config"
    assert "render_text" in refused.value.message
