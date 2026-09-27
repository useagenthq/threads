"""Which interface a card pins.

Pinning chooses, once, what we speak to a partner for the rest of the conversation. The failure to
guard against is pinning something we can never call: the send-time SSRF guard would catch it, but
by then every call on that remote answers NotSent forever, so an unusable URL has to lose the
selection here rather than win it and fail later.

Mirrors typescript/packages/a2a/test/pin.test.ts."""

from typing import Final

from cards import CARD_URL, as_bytes, card_of, rest_at, rpc_at

from threads.a2a.protocol import PinFailure, PinnedCard, pin_card

SAFE: Final = "https://partner.example/a2a"


def _pinned(interfaces: list[dict[str, str]]) -> PinnedCard:
    pinned = pin_card(as_bytes(card_of(supportedInterfaces=interfaces)), CARD_URL, has_bearer=False)
    assert isinstance(pinned, PinnedCard), pinned
    return pinned


class TestTheInterfaceACardPins:
    def test_a_usable_https_interface_is_pinned(self) -> None:
        assert _pinned([rpc_at(SAFE)]).wire.url == SAFE

    def test_a_url_with_credentials_loses_to_the_safe_one_behind_it(self) -> None:
        # The card offers the unusable URL FIRST, which is the whole point: taking the first match
        # pinned this, and every later call on the remote answered NotSent.
        candidates = [rpc_at("https://user:pass@10.0.0.1/a2a"), rpc_at(SAFE)]
        assert _pinned(candidates).wire.url == SAFE

    def test_a_private_ip_literal_loses_to_the_safe_one_behind_it(self) -> None:
        assert _pinned([rpc_at("https://10.0.0.1/a2a"), rpc_at(SAFE)]).wire.url == SAFE

    def test_a_loopback_literal_loses_in_either_family(self) -> None:
        candidates = [rpc_at("https://127.0.0.1/a2a"), rpc_at("https://[::1]/a2a"), rpc_at(SAFE)]
        assert _pinned(candidates).wire.url == SAFE

    def test_an_http_interface_loses_because_a_remote_is_called_over_https(self) -> None:
        candidates = [rpc_at("http://partner.example/a2a"), rpc_at(SAFE)]
        assert _pinned(candidates).wire.url == SAFE

    def test_a_less_preferred_binding_is_taken_when_the_preferred_one_is_unusable(self) -> None:
        # JSON-RPC is preferred, but this card's only JSON-RPC interface is one we can never call.
        pinned = _pinned([rpc_at("https://10.0.0.1/a2a"), rest_at(SAFE)])
        assert (pinned.wire.url, pinned.wire.binding) == (SAFE, "HTTP+JSON")

    def test_a_card_whose_every_interface_is_unusable_says_why_of_each(self) -> None:
        unusable = [
            rpc_at("https://user:pass@10.0.0.1/a2a"),
            rest_at("http://partner.example/a2a"),
        ]
        failed = pin_card(
            as_bytes(card_of(supportedInterfaces=unusable)), CARD_URL, has_bearer=False
        )
        assert isinstance(failed, PinFailure)
        assert failed.code == "remote_unsupported"
        assert "credentials" in failed.message
        assert "https" in failed.message

    def test_a_dns_name_is_not_judged_here(self) -> None:
        # Deciding a name now would pin a card against one moment's DNS, and a card is pinned once
        # and used for a long time. `vet` checks every address the name resolves to, at send time.
        internal = "https://internal-only.partner.example/a2a"
        assert _pinned([rpc_at(internal)]).wire.url == internal
