"""`remote()` and `bearer()` are pure config: the card is fetched and pinned later, so a host whose
partner is unreachable still starts. What is checked here is what a setup error must catch before
anything is sent, and that a credential is never anything but a header.

Mirrors typescript/packages/a2a/test/remote.test.ts."""

import pytest

from threadsai.a2a import DEFAULT_TIMEOUT_MS, bearer, remote
from threadsai.agents.config import ConfigError
from threadsai.secrets import secret

CARD = "https://partner.example/card.json"
TWO_MINUTES_MS = 120_000
FIVE_SECONDS_MS = 5_000
FIVE_CENTS_NANOS = 50_000_000


class TestRemote:
    def test_it_does_no_io_so_a_partner_that_is_down_does_not_stop_a_host_starting(self) -> None:
        # If this ever dialled, the test would hang or raise: there is no server at that name.
        refunds = remote("refunds", "https://nothing.invalid/.well-known/agent-card.json")
        assert refunds.name == "refunds"
        assert refunds.card_url == "https://nothing.invalid/.well-known/agent-card.json"

    def test_the_defaults_are_opaque_no_declared_cost_and_a_two_minute_deadline(self) -> None:
        refunds = remote("refunds", CARD)
        assert refunds.provenance == "opaque"
        assert refunds.cost_per_message == 0
        assert refunds.timeout_ms == DEFAULT_TIMEOUT_MS
        assert DEFAULT_TIMEOUT_MS == TWO_MINUTES_MS
        assert refunds.auth is None

    @pytest.mark.parametrize("name", ["Refunds", "refunds-desk", "1refunds", "", "refunds "])
    def test_a_name_that_is_not_an_agent_name_is_invalid_config(self, name: str) -> None:
        with pytest.raises(ConfigError) as raised:
            remote(name, CARD)
        assert raised.value.code == "invalid_config"
        assert "[a-z][a-z0-9_]*" in raised.value.message

    @pytest.mark.parametrize(
        "url", ["http://partner.example/card.json", "file:///tmp/card.json", "nonsense"]
    )
    def test_a_card_url_that_is_not_https_is_invalid_config(self, url: str) -> None:
        # A card is never fetched in the clear.
        with pytest.raises(ConfigError) as raised:
            remote("refunds", url)
        assert raised.value.code == "invalid_config"

    @pytest.mark.parametrize("timeout_ms", [0, -1])
    def test_a_deadline_that_is_not_positive_is_invalid_config(self, timeout_ms: int) -> None:
        with pytest.raises(ConfigError):
            remote("refunds", CARD, timeout_ms=timeout_ms)

    def test_a_deadline_that_is_given_is_kept(self) -> None:
        assert remote("refunds", CARD, timeout_ms=FIVE_SECONDS_MS).timeout_ms == FIVE_SECONDS_MS

    def test_a_declared_cost_is_whole_nanos_as_usd_gives_and_never_negative(self) -> None:
        with pytest.raises(ConfigError):
            remote("refunds", CARD, cost_per_message=-1)
        # 5 cents in nanos: what one message to this partner costs us, as declared.
        kept = remote("refunds", CARD, cost_per_message=FIVE_CENTS_NANOS)
        assert kept.cost_per_message == FIVE_CENTS_NANOS

    def test_provenance_none_sends_nothing_and_opaque_is_the_default(self) -> None:
        assert remote("refunds", CARD, provenance="none").provenance == "none"


class TestBearer:
    def test_it_resolves_its_secret_from_the_host_at_use_time(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        auth = bearer(secret("A2A_TEST_TOKEN"))
        assert auth.kind == "bearer"
        # Nothing was read yet, so an unset variable is not a problem until someone asks.
        monkeypatch.setenv("A2A_TEST_TOKEN", "a-partner-token")
        assert auth.reveal() == "a-partner-token"

    def test_an_unset_variable_is_missing_secret_not_an_empty_header(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("A2A_TEST_TOKEN_UNSET", raising=False)
        auth = bearer(secret("A2A_TEST_TOKEN_UNSET"))
        with pytest.raises(ConfigError) as raised:
            auth.reveal()
        assert raised.value.code == "missing_secret"
        assert "A2A_TEST_TOKEN_UNSET" in raised.value.message
