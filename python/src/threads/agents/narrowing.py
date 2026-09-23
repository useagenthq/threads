"""Setup checks for the agents another agent may start: a subagent only
narrows its parent: its tools are filtered to the parent's pinned names (Definition.allowed), it
can't need a sandbox or an egress path the parent lacks, and its permissions are narrowed at
dispatch by the parent's as a ceiling. A handoff target is
the one exception: it runs under its own pinned policy, capped by the principal and host."""

from threads.agents.config import ConfigError
from threads.agents.definition import Definition


def check[D](parent: Definition[D]) -> None:
    for kind, agents in (("subagent", parent.subagents), ("handoff", parent.handoffs)):
        names = [a.name for a in agents]
        if len(set(names)) != len(names):
            raise ConfigError("duplicate_name", f"{kind} names repeat: {names}")
    for child in parent.subagents:
        _narrows(parent, child)


def _narrows[D](parent: Definition[D], child: Definition[None]) -> None:
    if child.handoffs:
        raise ConfigError("invalid_config", f"subagent {child.name} can't hand off")
    if child.sandbox is not None and (
        parent.sandbox is None or child.sandbox.info.provider != parent.sandbox.info.provider
    ):
        raise ConfigError("invalid_config", f"subagent {child.name} needs its parent's sandbox")
    if child.egress == "unenforced" and parent.egress != "unenforced":
        raise ConfigError("invalid_config", f"subagent {child.name} widens egress")
