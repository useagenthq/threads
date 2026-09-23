"""Setup checks for the agents another agent may start: a subagent only
narrows its parent: its tools are filtered to the parent's pinned names (Definition.allowed), it
can't need a sandbox or an egress path the parent lacks, and its permissions are narrowed at
dispatch by the parent's as a ceiling. A handoff target is
the one exception: it runs under its own pinned policy, capped by the principal and host, and,
when a subagent hands off, by that subagent's ceilings too (spec/schema/README.md, Handoff
scope)."""

from threads.agents.config import ConfigError
from threads.agents.definition import Definition
from threads.log import Budget
from threads.loop.budget import unbounded


def check[D](parent: Definition[D]) -> None:
    for kind, agents in (("subagent", parent.subagents), ("handoff", parent.handoffs)):
        names = [a.name for a in agents]
        if len(set(names)) != len(names):
            raise ConfigError("duplicate_name", f"{kind} names repeat: {names}")
    for child in parent.subagents:
        _narrows(parent, child)


def _narrows[D](parent: Definition[D], child: Definition[None]) -> None:
    if child.sandbox is not None and (
        parent.sandbox is None or child.sandbox.info.provider != parent.sandbox.info.provider
    ):
        raise ConfigError("invalid_config", f"subagent {child.name} needs its parent's sandbox")
    if child.egress == "unenforced" and parent.egress != "unenforced":
        raise ConfigError("invalid_config", f"subagent {child.name} widens egress")


def enforceable[D](definition: Definition[D], covering: tuple[Budget, ...] = ()) -> None:
    """budget_unenforceable (spec/schema/README.md, Budget enforcement): every limit covering an
    agent's threads (its own budget, and every budget over the agents that start it) needs a
    per-attempt bound from its model, unless that agent stops on unknown usage."""
    over = covering if definition.budget is None else (*covering, definition.budget)
    info = definition.model.info
    for budget in over if definition.on_unknown_usage != "stop" else ():
        limit = unbounded(budget, info.limits, info.params.get("max_tokens"))
        if limit is not None:
            why = f"agent {definition.name}: its model has no per-attempt bound for {limit}"
            raise ConfigError("budget_unenforceable", why)
    for child in (*definition.subagents, *definition.handoffs):
        enforceable(child, over)


def run_budget[D](definition: Definition[D], budget: Budget | None) -> None:
    """A run budget covers the whole tree the run starts: checked as setup at run start."""
    if budget is not None:
        enforceable(definition, (budget,))
