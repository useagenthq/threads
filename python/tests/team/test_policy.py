"""The host's message_policy as pure rules (spec/api.json host.message_policy): what an agent may
do, which team tools that gives it, whether it is a lead, and the per-hop budget minimum. Mirrors
TypeScript's core/test/team/policy.test.ts."""

from collections.abc import Sequence

import pytest

from threads import MessagePolicyRule
from threads.agents.config import ConfigError
from threads.team.policy import (
    check_message_policy,
    leads,
    rule_for,
    rule_starts,
    rules_from,
    startable,
    team_tools,
)

AGENTS = ("support", "billing", "hr")


def rule(sender: str, to: str, allow: Sequence[str]) -> MessagePolicyRule:
    return {"from": sender, "to": to, "allow": [*allow]}  # pyright: ignore[reportReturnType]


class Named:
    """The least a startable agent is: a name."""

    def __init__(self, name: str) -> None:
        self.name = name


@pytest.mark.parametrize(
    "bad", [rule("nobody", "billing", ["ask"]), rule("support", "nobody", ["ask"])]
)
def test_a_from_or_to_that_names_no_host_agent_is_invalid_config(bad: MessagePolicyRule) -> None:
    with pytest.raises(ConfigError) as caught:
        check_message_policy([bad], AGENTS)
    assert caught.value.code == "invalid_config"
    assert "is not a host agent" in str(caught.value)


def test_an_empty_allow_is_invalid_config_naming_the_rule() -> None:
    with pytest.raises(ConfigError) as caught:
        check_message_policy([rule("support", "billing", [])], AGENTS)
    assert caught.value.code == "invalid_config"
    assert str(caught.value) == (
        "invalid_config: message_policy rule support -> billing: allow is empty; "
        "list what support may do, or drop the rule"
    )


def test_a_repeated_from_to_pair_is_duplicate_name() -> None:
    rules = [rule("support", "billing", ["ask"]), rule("support", "billing", ["send"])]
    with pytest.raises(ConfigError) as caught:
        check_message_policy(rules, AGENTS)
    assert caught.value.code == "duplicate_name"
    assert "is listed twice" in str(caught.value)


def test_the_same_pair_each_way_and_two_pairs_of_one_from_are_fine() -> None:
    check_message_policy(
        [
            rule("support", "billing", ["ask"]),
            rule("billing", "support", ["send"]),
            rule("support", "hr", ["ask"]),
        ],
        AGENTS,
    )


RULES = [
    rule("support", "billing", ["ask"]),
    rule("support", "hr", ["monitor"]),
    rule("hr", "billing", ["start", "send"]),
]


def test_only_the_rules_with_the_agent_as_from() -> None:
    assert [r["to"] for r in rules_from(RULES, "support")] == ["billing", "hr"]
    assert rules_from(RULES, "billing") == ()


def test_one_team_tool_per_allowed_op_and_wait_comes_with_monitor() -> None:
    def of(name: str) -> frozenset[str]:
        return team_tools(None, False, rules_from(RULES, name))

    assert of("support") == frozenset({"ask", "monitor", "wait"})
    assert of("hr") == frozenset({"send", "start"})
    assert team_tools(None, False, ()) == frozenset()


def test_a_lead_and_a_member_get_all_seven_whatever_their_rules_say() -> None:
    seven = frozenset({"ask", "cancel", "monitor", "reply", "send", "start", "wait"})
    assert team_tools((), False, ()) == seven
    assert team_tools(None, True, ()) == seven


def test_a_rule_allowing_start_makes_its_from_a_lead_but_ask_alone_does_not() -> None:
    assert leads(None, rules_from(RULES, "hr")) is True
    assert leads(None, rules_from(RULES, "support")) is False
    # An agent with a team of its own is a lead whatever its rules say.
    assert leads((), rules_from(RULES, "support")) is True


def test_start_may_name_the_teams_agents_then_the_ones_a_rule_adds_each_once() -> None:
    billing, support = Named("billing"), Named("support")
    assert rule_starts(rules_from(RULES, "hr")) == ("billing",)
    assert startable([billing], rules_from(RULES, "hr"), [billing]) == (billing,)
    assert startable([support], rules_from(RULES, "hr"), [billing]) == (support, billing)
    assert startable(None, rules_from(RULES, "support"), [billing]) == ()


def test_a_rule_is_found_by_from_to_and_op_never_by_an_op_it_omits() -> None:
    found = rule_for(RULES, "support", "billing", "ask")
    assert found is not None
    assert found["to"] == "billing"
    assert rule_for(RULES, "support", "billing", "send") is None
    assert rule_for(RULES, "billing", "support", "ask") is None
