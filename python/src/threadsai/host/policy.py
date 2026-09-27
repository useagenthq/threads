"""host(message_policy=...) bound to the host's agents (lane 29C): each agent's definition carries
the rules with it as `from`, plus the agents a start rule names, since a rule-only lead's own team
is empty."""

from collections.abc import Collection, Mapping, Sequence
from dataclasses import replace

from threadsai.agents.agent import Agent
from threadsai.agents.definition import Definition
from threadsai.team.policy import MessagePolicyRule, check_message_policy, rule_starts, rules_from


def ruled(
    agents: Mapping[str, Agent[None, object]],
    message_policy: Sequence[MessagePolicyRule],
    members: Collection[str] = (),
) -> dict[str, Definition[None]]:
    """Every host agent's definition under the host's rules, with the host's members (Teams
    Phase 2), which a thread opens its tenant's host team from before addressing one. Raises
    ConfigError for a rule that names no host agent, an empty allow, or a repeated (from, to)
    pair."""
    named = {a.definition.name: a.definition for a in agents.values()}
    check_message_policy(message_policy, named)
    out: dict[str, Definition[None]] = {}
    for key, agent in agents.items():
        definition = agent.definition
        rules = rules_from(message_policy, definition.name)
        if not rules:
            out[key] = definition
            continue
        listed = tuple(d for n, d in named.items() if n in rule_starts(rules))
        out[key] = replace(definition, rules=rules, rule_agents=listed)
    if not members:
        return out
    # The host members every definition carries are the *ruled* ones: a host member's own rules
    # decide its calls and shape its pin, so the pin that opens the host team and the pin its
    # rebind makes must be the same. host_members itself is host policy and is never pinned.
    by_name = {d.name: d for d in out.values()}
    hosted = tuple(by_name[n] for n in members if n in by_name)
    return {key: replace(d, host_members=hosted) for key, d in out.items()}
