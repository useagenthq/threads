"""Live gate: one real send per channel adapter. Skipped unless THREADS_LIVE=1 and that
channel's secrets and destination are set; never part of the offline suite.

    THREADS_LIVE=1 \\
    SLACK_BOT_TOKEN=... THREADS_LIVE_SLACK_CHANNEL=C123 \\
    WHATSAPP_ACCESS_TOKEN=... THREADS_LIVE_WHATSAPP_PHONE_ID=... THREADS_LIVE_WHATSAPP_TO=1555... \\
    GITHUB_TOKEN=... THREADS_LIVE_GITHUB_ISSUE=owner/repo#1 \\
    uv run pytest -m live tests/adapters/channels
"""

import asyncio
import os
import uuid

import pytest

from threads.github import github
from threads.host import ChannelAdapter, DeliveryOutcome, Sent
from threads.memory.fence import bound
from threads.secrets import secret
from threads.slack import slack
from threads.whatsapp import whatsapp

pytestmark = pytest.mark.live


@pytest.fixture(autouse=True)
def live_gate() -> None:
    if os.environ.get("THREADS_LIVE") != "1":
        pytest.skip("live gate: set THREADS_LIVE=1 to send real requests")


def _needs(*names: str) -> tuple[str, ...]:
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        pytest.skip(f"live gate: {', '.join(missing)} not set")
    return tuple(os.environ[n] for n in names)


def _send(channel: ChannelAdapter, address: str, token_name: str) -> DeliveryOutcome:
    async def allowed() -> bool:
        return True

    async def run() -> DeliveryOutcome:
        with bound(allowed):
            op = {"text": "threads live gate", "address": address}
            credentials = dict.fromkeys(channel.secrets, os.environ[token_name])
            return await channel.perform(op, f"live:{uuid.uuid4().hex}", credentials)

    return asyncio.run(run())


def test_slack_sends() -> None:
    (address,) = _needs("SLACK_BOT_TOKEN", "THREADS_LIVE_SLACK_CHANNEL")[1:]
    channel = slack(
        signing_secret=secret("SLACK_SIGNING_SECRET"),
        bot_token=secret("SLACK_BOT_TOKEN"),
        agent="a",
    )
    assert isinstance(_send(channel, address, "SLACK_BOT_TOKEN"), Sent)


def test_whatsapp_sends() -> None:
    names = ("WHATSAPP_ACCESS_TOKEN", "THREADS_LIVE_WHATSAPP_PHONE_ID", "THREADS_LIVE_WHATSAPP_TO")
    _, phone, to = _needs(*names)
    channel = whatsapp(
        app_secret=secret("WHATSAPP_APP_SECRET"),
        access_token=secret("WHATSAPP_ACCESS_TOKEN"),
        verify_token=secret("WHATSAPP_VERIFY_TOKEN"),
        phone_number_id=phone,
        agent="a",
    )
    assert isinstance(_send(channel, to, "WHATSAPP_ACCESS_TOKEN"), Sent)


def test_github_comments() -> None:
    (issue,) = _needs("GITHUB_TOKEN", "THREADS_LIVE_GITHUB_ISSUE")[1:]
    channel = github(
        webhook_secret=secret("GITHUB_WEBHOOK_SECRET"), token=secret("GITHUB_TOKEN"), agent="a"
    )
    assert isinstance(_send(channel, issue, "GITHUB_TOKEN"), Sent)
