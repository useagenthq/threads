"""Setup checks for the agents another agent may start: a subagent only
narrows its parent, so it can't name a tool, a sandbox or an egress path the parent lacks. Its
permissions are narrowed at dispatch instead, by the parent's as a ceiling. A handoff target is
the one exception: it runs under its own pinned policy, capped by the principal and host."""

from collections.abc import Sequence

from threads.agents.config import ConfigError
from threads.agents.definition import Definition
from threads.log import ToolSpec
from threads.tools import FRAMEWORK, HOST


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
    extra = _names(child.specs()) - _names(parent.specs())
    if extra:
        raise ConfigError(
            "invalid_config", f"subagent {child.name} has tools its parent lacks: {sorted(extra)}"
        )
    if child.sandbox is not None and (
        parent.sandbox is None or child.sandbox.info.provider != parent.sandbox.info.provider
    ):
        raise ConfigError("invalid_config", f"subagent {child.name} needs its parent's sandbox")
    if child.egress == "unenforced" and parent.egress != "unenforced":
        raise ConfigError("invalid_config", f"subagent {child.name} widens egress")


def _names(specs: Sequence[ToolSpec]) -> set[str]:
    """Tool names that grant something: the framework tools and read_tool_result change only
    the log, so any thread may have them."""
    return {s.name for s in specs} - FRAMEWORK - HOST
