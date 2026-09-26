"""host(message_policy=...) bound to the host's agents (lane 29C): each agent's definition carries
the rules with it as `from`, plus the agents a start rule names, since a rule-only lead's own team
is empty."""

from collections.abc import Mapping, Sequence
from dataclasses import replace

from threads.agents.agent import Agent
from threads.agents.definition import Definition
from threads.team.policy import MessagePolicyRule, check_message_policy, rule_starts, rules_from


def ruled(
    agents: Mapping[str, Agent[None, object]], message_policy: Sequence[MessagePolicyRule]
) -> dict[str, Definition[None]]:
    """Every host agent's definition under the host's rules. Raises ConfigError for a rule that
    names no host agent, an empty allow, or a repeated (from, to) pair."""
    named = {a.definition.name: a.definition for a in agents.values()}
    check_message_policy(message_policy, named)
    out: dict[str, Definition[None]] = {}
    for key, agent in agents.items():
        definition = agent.definition
        rules = rules_from(message_policy, definition.name)
        starts = rule_starts(rules)
        listed = tuple(d for n, d in named.items() if n in starts)
        out[key] = definition if not rules else replace(definition, rules=rules, rule_agents=listed)
    return out
