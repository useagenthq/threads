"""`host(a2a=...)` at ready(), and the card it publishes.

Every refusal here names a fix the operator makes in the config. The approvers rule is the one that
matters: an agent a partner can reach, whose tools can act, must say who approves those actions —
never the calling partner. Mirrors typescript/packages/host/test/a2a/setup.test.ts and card.test.ts.
"""

import json
from collections.abc import Sequence

import pytest
from pydantic import BaseModel, JsonValue

from threads import Agent, RunContext, agent, scripted_model, tool
from threads.agents.config import ConfigError
from threads.host.a2a.card import card_bytes
from threads.host.a2a.config import A2aOptions, default_budget
from threads.host.a2a.setup import expose_a2a
from threads.log import Principal

USAGE: JsonValue = {"input_tokens": 10, "output_tokens": 2}
LEAD = Principal(issuer="api", tenant="acme", subject="support-leads@acme.example")
ORIGIN = "https://acme.example"
ONE: A2aOptions = {"expose": {"support": {"description": "Customer support for Acme orders."}}}
DAY_MS = 7 * 24 * 60 * 60 * 1000
ONE_DOLLAR_NANOS = 1_000_000_000
TEN_MINUTES_MS = 600_000
FIVE_SECONDS_MS = 5_000


def _text(reply: str) -> JsonValue:
    return {"content": [{"type": "text", "text": reply}], "stop_reason": "end_turn", "usage": USAGE}


def _script() -> dict[str, JsonValue]:
    return {"responses": [_text("Hi.")]}


class Note(BaseModel):
    text: str


def talker(*, instructions: str = "Be helpful and secret.") -> Agent[None, str]:
    """An agent whose only tools read, so it needs no approvers."""
    return agent(name="support", model=scripted_model(_script()), instructions=instructions)


def sender(*, approvers: Sequence[Principal] | None = None) -> Agent[None, str]:
    """An agent with a tool that acts, which is what the approvers rule is about."""

    async def send(args: Note, _ctx: RunContext[None]) -> str:
        return f"sent {args.text}"

    acts = tool(
        name="send_email",
        description="Send an email.",
        input=Note,
        runs="host",
        execute=send,
        effect="unguarded",
    )
    if approvers is None:
        return agent(name="support", model=scripted_model(_script()), tools=[acts])
    return agent(
        name="support", model=scripted_model(_script()), tools=[acts], approvers=list(approvers)
    )


class TestTheApproversRule:
    def test_an_agent_whose_tools_can_act_must_name_who_approves(self) -> None:
        with pytest.raises(ConfigError) as raised:
            expose_a2a(ONE, {"support": sender()})
        assert raised.value.code == "invalid_config"
        assert "add approvers to agent 'support'" in raised.value.message

    def test_naming_approvers_is_enough_and_the_partner_is_never_one_of_them(self) -> None:
        exposed = expose_a2a(ONE, {"support": sender(approvers=[LEAD])})
        assert set(exposed.agents) == {"support"}

    def test_an_agent_whose_tools_only_read_needs_no_approvers(self) -> None:
        # The rule is about actions that can need approval, not about being exposed at all.
        exposed = expose_a2a(ONE, {"support": talker()})
        assert set(exposed.agents) == {"support"}


class TestTheOtherSetupRefusals:
    def test_exposing_an_agent_the_host_does_not_run_is_refused(self) -> None:
        with pytest.raises(ConfigError) as raised:
            expose_a2a({"expose": {"missing": {"description": "d"}}}, {"support": talker()})
        assert raised.value.code == "invalid_config"
        assert "a2a.expose.missing names no host agent" in raised.value.message

    def test_an_agent_with_handoffs_cannot_be_exposed(self) -> None:
        # A handoff moves the conversation to another thread, which a remote task cannot follow.
        other = agent(name="billing", model=scripted_model(_script()))
        handing = agent(name="support", model=scripted_model(_script()), handoffs=[other])
        with pytest.raises(ConfigError) as raised:
            expose_a2a(ONE, {"support": handing})
        assert raised.value.code == "invalid_config"
        assert "has handoffs and can't be exposed" in raised.value.message

    def test_an_empty_description_is_refused_because_the_card_requires_one(self) -> None:
        with pytest.raises(ConfigError) as raised:
            expose_a2a({"expose": {"support": {"description": ""}}}, {"support": talker()})
        assert raised.value.code == "invalid_config"


class TestTheDefaultBudget:
    def test_an_exposed_task_has_a_ceiling_even_when_the_operator_sets_none(self) -> None:
        exposed = expose_a2a(ONE, {"support": talker()})
        assert exposed.agents["support"].budget == default_budget()
        # A dollar and ten minutes: an exposed agent is an internet-facing spend path.
        assert default_budget().max_cost_nanos == ONE_DOLLAR_NANOS
        assert default_budget().max_wall_ms == TEN_MINUTES_MS

    def test_an_operator_may_set_its_own(self) -> None:
        options: A2aOptions = {
            "expose": {"support": {"description": "d", "budget": {"max_wall_ms": FIVE_SECONDS_MS}}}
        }
        exposed = expose_a2a(options, {"support": talker()})
        assert exposed.agents["support"].budget.max_wall_ms == FIVE_SECONDS_MS


class TestTheCard:
    def _bytes(self, bot: Agent[None, str]) -> bytes:
        exposed = expose_a2a(ONE, {"support": bot})
        return card_bytes(exposed, "support", exposed.agents["support"], ORIGIN)

    def _card(self, bot: Agent[None, str]) -> dict[str, JsonValue]:
        parsed: JsonValue = json.loads(self._bytes(bot))
        assert isinstance(parsed, dict)
        return parsed

    def test_it_publishes_no_instructions_no_tool_names_and_no_model_name(self) -> None:
        # The one place where saying too much is the failure.
        text = self._bytes(sender(approvers=[LEAD])).decode()
        assert "secret" not in text
        assert "send_email" not in text
        assert "scripted" not in text

    def test_it_declares_both_bindings_at_this_origin_and_nothing_else(self) -> None:
        card = self._card(talker())
        interfaces = card["supportedInterfaces"]
        assert interfaces == [
            {
                "protocolBinding": "JSONRPC",
                "protocolVersion": "1.0",
                "url": f"{ORIGIN}/a2a/support",
            },
            {
                "protocolBinding": "HTTP+JSON",
                "protocolVersion": "1.0",
                "url": f"{ORIGIN}/a2a/support",
            },
        ]

    def test_it_states_the_cuts_rather_than_leaving_them_to_the_docs(self) -> None:
        capabilities = self._card(talker())["capabilities"]
        assert isinstance(capabilities, dict)
        assert capabilities["streaming"] is True
        assert capabilities["pushNotifications"] is False
        assert capabilities["extendedAgentCard"] is False

    def test_it_declares_our_idempotent_send_extension_with_its_window(self) -> None:
        capabilities = self._card(talker())["capabilities"]
        assert isinstance(capabilities, dict)
        extensions = capabilities["extensions"]
        assert isinstance(extensions, list)
        first = extensions[0]
        assert isinstance(first, dict)
        assert first["uri"] == "https://threadsai.dev/a2a/ext/idempotent-send/v1"
        assert first["params"] == {"window_ms": DAY_MS}

    def test_its_version_is_the_agents_config_hash_so_it_changes_when_the_agent_does(self) -> None:
        one = self._card(talker(instructions="One."))
        two = self._card(talker(instructions="Two."))
        assert one["version"] != two["version"]
        # And a hash publishes nothing about the configuration it names.
        assert "One." not in json.dumps(one)

    def test_its_one_skill_carries_the_tags_the_proto_requires(self) -> None:
        skills = self._card(talker())["skills"]
        assert isinstance(skills, list)
        first = skills[0]
        assert isinstance(first, dict)
        assert first["tags"] == ["text"]

    def test_the_default_security_scheme_is_bearer_and_an_operator_may_replace_it(self) -> None:
        assert self._card(talker())["securitySchemes"] == {
            "bearer": {"httpAuthSecurityScheme": {"scheme": "bearer"}}
        }
        options: A2aOptions = {
            "expose": {"support": {"description": "d"}},
            "security_schemes": {"corp": {"oauth2SecurityScheme": {}}},
        }
        exposed = expose_a2a(options, {"support": talker()})
        card = json.loads(card_bytes(exposed, "support", exposed.agents["support"], ORIGIN))
        assert isinstance(card, dict)
        assert card["securitySchemes"] == {"corp": {"oauth2SecurityScheme": {}}}
        # Derived from the declared names, not hardcoded to bearer: a custom scheme set gets its
        # requirement too.
        assert card["securityRequirements"] == [{"schemes": {"corp": {"list": []}}}]

    def test_a_declared_scheme_comes_with_the_requirement_that_says_it_is_required(self) -> None:
        # Declaring a scheme and no requirement is what a conforming partner reads as "nothing is
        # required": it calls unauthenticated and gets our 401. Ours infers "auth required" from the
        # scheme set being non-empty, which is exactly why this was invisible from inside.
        card = self._card(talker())
        assert card["securityRequirements"] == [{"schemes": {"bearer": {"list": []}}}]

    def test_the_same_config_and_origin_always_publish_the_same_bytes(self) -> None:
        bot = talker()
        assert self._bytes(bot) == self._bytes(bot)
