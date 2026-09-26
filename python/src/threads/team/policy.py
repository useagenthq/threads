"""The host's message_policy (spec/api.json host.message_policy and MessagePolicyRule): rules that
decide after a team's grant and before default deny, for every op of every sender. They only add,
so a team's own grant is never narrowed. Which team tools an agent is offered, and whether it is a
lead at all, follow from its rules the same way."""

from collections.abc import Collection, Sequence
from typing import Final, Literal, NotRequired, Protocol, TypedDict

from threads._tool_names import PINNED_MEMBERS
from threads.agents.config import ConfigError
from threads.log import Budget

type MessagePolicyOp = Literal["start", "send", "ask", "monitor", "cancel"]
"""What a rule may allow; wait is decided as monitor."""

MessagePolicyRule = TypedDict(
    "MessagePolicyRule",
    {
        "from": str,
        "to": str,
        "allow": Sequence[MessagePolicyOp],
        "budget": NotRequired[Budget],
    },
)
"""One host message_policy rule. Anything no rule or team grant allows is denied. budget caps each
member `from` starts, whose budget is the smaller of this and start's, and each turn a send or ask
to a host member opens."""

_TOOLS: Final[dict[MessagePolicyOp, frozenset[str]]] = {
    "start": frozenset({"start"}),
    "send": frozenset({"send"}),
    "ask": frozenset({"ask"}),
    "monitor": frozenset({"monitor", "wait"}),
    "cancel": frozenset({"cancel"}),
}
"""The team tool each op gives its `from`; wait comes with monitor."""


class Named(Protocol):
    @property
    def name(self) -> str: ...


def rules_from(rules: Sequence[MessagePolicyRule], agent: str) -> tuple[MessagePolicyRule, ...]:
    """The rules with `agent` as the acting side, in configured order."""
    return tuple(r for r in rules if r["from"] == agent)


def rule_for(
    rules: Sequence[MessagePolicyRule], sender: str, target: str, op: MessagePolicyOp
) -> MessagePolicyRule | None:
    """The rule that lets `sender` do `op` to `target`. One (from, to) pair exists at most once."""
    for rule in rules:
        if rule["from"] == sender and rule["to"] == target and op in rule["allow"]:
            return rule
    return None


def team_tools(
    own: Sequence[Named] | None, member: bool, rules: Sequence[MessagePolicyRule]
) -> frozenset[str]:
    """The team tools a pin offers: all seven for a lead (agent(team=...)) and for a team's member;
    for an agent with host rules and no team of its own, one per op some rule with it as `from`
    allows, so its line 0 never shows a tool that is always denied."""
    if own is not None or member:
        return PINNED_MEMBERS
    return frozenset[str]().union(*(_TOOLS[op] for r in rules for op in r["allow"]))


def rule_starts(rules: Sequence[MessagePolicyRule]) -> tuple[str, ...]:
    """The agents an agent's rules let it start. A rule allowing start makes its from a lead."""
    return tuple(r["to"] for r in rules if "start" in r["allow"])


def check_message_policy(rules: Sequence[MessagePolicyRule], agents: Collection[str]) -> None:
    """host(message_policy=...) at setup: every from and to names a host agent, every allow says
    something, and each (from, to) pair appears once. Raises ConfigError."""
    seen: set[tuple[str, str]] = set()
    for rule in rules:
        at = f"message_policy rule {rule['from']} -> {rule['to']}"
        for side in (rule["from"], rule["to"]):
            if side not in agents:
                named = ", ".join(sorted(agents))
                raise ConfigError(
                    "invalid_config", f"{at}: {side} is not a host agent; name one of {named}"
                )
        if not rule["allow"]:
            raise ConfigError(
                "invalid_config",
                f"{at}: allow is empty; list what {rule['from']} may do, or drop the rule",
            )
        pair = (rule["from"], rule["to"])
        if pair in seen:
            raise ConfigError(
                "duplicate_name", f"{at} is listed twice; put every op of one pair in one rule"
            )
        seen.add(pair)


def leads(team: Sequence[Named] | None, rules: Sequence[MessagePolicyRule]) -> bool:
    """Whether an agent runs as a lead: it has a team of its own, or a rule lets it start. An
    agent whose rules target only host members is no lead; it is a caller, in no team (29D)."""
    return team is not None or bool(rule_starts(rules))


def startable[T: Named](
    team: Sequence[T] | None, rules: Sequence[MessagePolicyRule], agents: Sequence[T]
) -> tuple[T, ...]:
    """The agents start may name: those the team lists, then those a rule adds, each once."""
    own = tuple(team or ())
    listed = {a.name for a in own}
    adds = rule_starts(rules)
    return (*own, *(a for a in agents if a.name in adds and a.name not in listed))
