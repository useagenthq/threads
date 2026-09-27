"""`host(members=...)` (spec/api.json `host.members`, `HostMemberOptions`): the host agents that
run as one long-lived member per tenant, and the setup refusals a host member brings.

A host member is shared by every conversation of its tenant, so what would park it or ask one
conversation's user a question is refused here, before anything is written.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal

from threads.agents.agent import Agent
from threads.agents.config import ConfigError
from threads.agents.definition import Definition
from threads.log import Threshold2
from threads.loop.defaults import CONTEXT
from threads.pos_int import is_pos_int
from threads.team.policy import MessagePolicyOp, MessagePolicyRule
from threads.team.supervise import RestartPolicy

WHOLE_WINDOW: Final = 1000
"""A compaction trigger in permille of the whole context window."""

MIN_WINDOW_MS: Final = 1000
"""`within_ms` is a sliding window of restarts, so anything below a second is a mistake."""

_AROUND_A_HOST_MEMBER: Final[frozenset[MessagePolicyOp]] = frozenset({"send", "ask"})
"""All a rule may allow when either side of it is a host member: the host alone starts, watches
and stops one, and a host member leads no team, so it has nothing to start, watch or stop."""


@dataclass(frozen=True, slots=True)
class HostMemberOptions:
    """spec/api.json `HostMemberOptions`: how the host supervises one host member."""

    restart: Literal["on_failure", "never"] = "on_failure"
    max_restarts: int = 3
    within_ms: int = 60_000


def restart_policy(options: HostMemberOptions) -> RestartPolicy:
    """The public option as the supervisor step and the log read it (rule 51's `policy`)."""
    return RestartPolicy(options.restart, options.max_restarts, options.within_ms)


def check_members(
    agents: Mapping[str, Agent[None, object]],
    members: Mapping[str, HostMemberOptions],
    rules: Sequence[MessagePolicyRule],
) -> None:
    """`host()`'s refusals for its `members` option and the rules around it. Raises ConfigError,
    each naming `members.<name>` or the rule."""
    for name, options in members.items():
        agent = agents.get(name)
        if agent is None:
            named = ", ".join(sorted(agents)) or "none"
            raise ConfigError(
                "invalid_config",
                f"members.{name}: this host defines no agent {name}; name one of {named}",
            )
        _options(name, options)
        _agent(name, agent.definition)
    _rules(members, rules)


def _options(name: str, options: HostMemberOptions) -> None:
    for field, value in (("max_restarts", options.max_restarts), ("within_ms", options.within_ms)):
        if not is_pos_int(value):
            raise ConfigError(
                "invalid_config", f"members.{name}.{field} is {value!r}; give a positive integer"
            )
    if options.within_ms < MIN_WINDOW_MS:
        raise ConfigError(
            "invalid_config",
            f"members.{name}.within_ms is {options.within_ms}; the restart window is at least"
            f" {MIN_WINDOW_MS} ms",
        )


def _agent(name: str, definition: Definition[None]) -> None:
    at = f"members.{name}"
    if definition.handoffs:
        raise ConfigError(
            "handoff_in_team",
            f"{at}: agent {name} lists handoffs, and a host member can't hand off; remove its"
            " handoffs or the members entry",
        )
    if definition.team is not None:
        raise ConfigError(
            "invalid_config",
            f"{at}: agent {name} has a team of its own, and a host member leads none; remove its"
            " team or the members entry",
        )
    if _compaction_off(definition):
        raise ConfigError(
            "invalid_config",
            f"{at}: agent {name} turns compaction off, and a host member's history never ends;"
            " leave its context.compact.trigger where compaction still fires",
        )
    if _needs_approvers(definition) and not definition.approvers:
        raise ConfigError(
            "invalid_config",
            "a host member is shared by every conversation in the tenant, so its approvals need"
            f" approvers; add approvers to agent '{name}'",
        )


def _compaction_off(definition: Definition[None]) -> bool:
    """A trigger that can only fire once the window is already full turns compaction off: the
    schema has no other way to say it (a Threshold is a positive number)."""
    trigger = (definition.context or CONTEXT).compact.trigger
    if isinstance(trigger, Threshold2):
        return trigger.permille >= WHOLE_WINDOW
    return trigger.tokens >= definition.model.info.limits.context_window


def _needs_approvers(definition: Definition[None]) -> bool:
    """A tool that can need approval, or a question only an approver can answer."""
    if definition.answerer:
        return True
    return any(t.spec().effect_class != "read_only" for t in definition.tools)


def _rules(members: Mapping[str, HostMemberOptions], rules: Sequence[MessagePolicyRule]) -> None:
    """The refusals 29C left for host({members}): a rule with a host member on either side
    allows only send and ask."""
    for rule in rules:
        to_host, from_host = rule["to"] in members, rule["from"] in members
        bad = sorted(set(rule["allow"]) - _AROUND_A_HOST_MEMBER)
        if not bad or not (to_host or from_host):
            continue
        at = f"message_policy rule {rule['from']} -> {rule['to']}"
        why = _why(rule, to_host=to_host, from_host=from_host)
        raise ConfigError("invalid_config", f"{at}: {why}; drop {', '.join(bad)} from its allow")


def _why(rule: MessagePolicyRule, *, to_host: bool, from_host: bool) -> str:
    """Why this rule may allow only send and ask, in the terms of the side that decides it."""
    if to_host and from_host:
        return "one host member may only send to and ask another"
    if to_host:
        return f"{rule['to']} is a host member, which the host starts, watches and stops"
    return (
        f"{rule['from']} is a host member, which leads no team and so has nothing to start,"
        " watch or cancel; a rule that allows start would make it a lead as well"
    )
