"""A host message (an ask_user question or its correction) is one plain text op on every
built-in channel, sent like a final reply."""

from typing import TYPE_CHECKING

import pytest

from threads.github import github
from threads.secrets import secret
from threads.slack import slack
from threads.whatsapp import whatsapp

if TYPE_CHECKING:
    from threads.host import ChannelAdapter

TEXT = "Which color?\n\n1. red\n2. blue"


@pytest.fixture(autouse=True)
def secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIGNING", "shh")
    monkeypatch.setenv("TOKEN", "tok")
    monkeypatch.setenv("VERIFY", "verify")


def test_a_host_message_is_one_text_op_on_every_channel() -> None:
    channels: list[ChannelAdapter] = [
        slack(signing_secret=secret("SIGNING"), bot_token=secret("TOKEN"), agent="a"),
        whatsapp(
            app_secret=secret("SIGNING"),
            access_token=secret("TOKEN"),
            verify_token=secret("VERIFY"),
            phone_number_id="p",
            agent="a",
        ),
        github(webhook_secret=secret("SIGNING"), token=secret("TOKEN"), agent="a"),
    ]
    for channel in channels:
        assert list(channel.render_text(TEXT)) == [{"text": TEXT}]
