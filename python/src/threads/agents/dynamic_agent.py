"""spec/api.json `DynamicAgent`: a template a lead's team lists, whose members its starter defines
at start (spec/schema/README.md, Teams, "Dynamic members"), and the definition such a member
runs."""

from dataclasses import replace

from threads.agents.config import UnboundError
from threads.agents.definition import Definition, Dynamic
from threads.log import MemberDefine
from threads.team.dynamic import KEPT


class DynamicAgent[D, O]:
    """spec/api.json `DynamicAgent`, from `dynamic_agent()`. It runs only as a team member: put it
    in a lead's team."""

    def __init__(self, definition: Definition[D]) -> None:
        self._definition = definition

    @property
    def name(self) -> str:
        """The template's name: the agent a start names."""
        return self._definition.name

    @property
    def definition(self) -> Definition[D]:
        """What every member of this template pins, before its starter's choice."""
        return self._definition


def member_definition[D](
    template: Definition[D], define: MemberDefine, starter: str
) -> Definition[D]:
    """The definition a member of `template` runs: the chosen model, its tools narrowed to the
    chosen ones and F by the pin's narrowing primitive, and the choice pinned. Raises
    ConfigError when the chosen model key is gone."""
    model = dict(template.models).get(define.model)
    if model is None:
        why = f"dynamic agent {template.name} has no model {define.model}"
        raise UnboundError("invalid_config", why)
    return replace(
        template,
        model=model,
        allowed=frozenset(define.tools) | KEPT,
        dynamic=Dynamic(template.name, define, starter),
    )
