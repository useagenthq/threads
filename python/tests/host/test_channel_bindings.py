"""host.ready() checks each channel factory's configuration: its agent must be a host agent
(invalid_config) and every secret it names must resolve (missing_secret). One case per channel."""

import asyncio

import pytest

from threads import ConfigError, agent, scripted_model, sqlite
from threads.github import github
from threads.host import ChannelAdapter, host
from threads.secrets import secret
from threads.slack import slack
from threads.whatsapp import whatsapp


def _slack(to: str) -> ChannelAdapter:
    return slack(signing_secret=secret("CH_ONE"), bot_token=secret("CH_TWO"), agent=to)


def _github(to: str) -> ChannelAdapter:
    return github(webhook_secret=secret("CH_ONE"), token=secret("CH_TWO"), agent=to)


def _whatsapp(to: str) -> ChannelAdapter:
    return whatsapp(
        app_secret=secret("CH_ONE"),
        access_token=secret("CH_TWO"),
        verify_token=secret("CH_THREE"),
        agent=to,
    )


CHANNELS = {"slack": _slack, "github": _github, "whatsapp": _whatsapp}


@pytest.fixture(autouse=True)
def secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("CH_ONE", "CH_TWO", "CH_THREE"):
        monkeypatch.setenv(name, f"{name.lower()}-value")


def _ready(name: str, channel: ChannelAdapter) -> ConfigError:
    bot = agent(model=scripted_model({"responses": []}))
    served = host(store=sqlite(":memory:"), agents={"support": bot}, channels={name: channel})
    with pytest.raises(ConfigError) as refused:
        asyncio.run(served.ready())
    return refused.value


@pytest.mark.parametrize("name", CHANNELS)
def test_ready_refuses_a_channel_whose_agent_is_not_a_host_agent(name: str) -> None:
    refused = _ready(name, CHANNELS[name]("sales"))
    assert refused.code == "invalid_config"
    assert name in str(refused)
    assert "sales" in str(refused)


@pytest.mark.parametrize("name", CHANNELS)
def test_ready_refuses_a_channel_whose_secret_is_unset(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CH_TWO")
    assert _ready(name, CHANNELS[name]("support")).code == "missing_secret"
