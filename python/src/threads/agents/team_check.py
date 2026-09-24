"""Setup refusals of a team (spec/api.json agent.team): a listed agent that hands off
(handoff_in_team), two agents of one name in the team tree, and a team thread's own tool taking a
team tool's name (duplicate_name)."""

from collections.abc import Sequence

from threads.agents.config import ConfigError
from threads.agents.definition import Definition
from threads.tools.specs import MEMBERS


def agents_of(
    team: Sequence[Definition[None]], into: dict[str, Definition[None]] | None = None
) -> dict[str, Definition[None]]:
    """Every agent of a team tree by name, nested teams included: materialize rebinds a member
    by its agent name."""
    found = {} if into is None else into
    for agent in team:
        known = found.get(agent.name)
        if known is agent:
            continue
        if known is not None:
            why = f"two agents in one team are named {agent.name}; give each a unique name"
            raise ConfigError("duplicate_name", why)
        found[agent.name] = agent
        agents_of(agent.team or (), found)
    return found


def check_team[D](lead: Definition[D]) -> None:
    """Raises ConfigError for a team that can't run: see the module."""
    if lead.team is None:
        return
    _team_names(lead)
    for name, agent in agents_of(lead.team).items():
        if agent.handoffs:
            why = (
                f"agent {name} lists handoffs, and a team member can't hand off; remove its "
                "handoffs or its place in the team"
            )
            raise ConfigError("handoff_in_team", why)
        _team_names(agent)


def _team_names[D](agent: Definition[D]) -> None:
    """A team thread's own tools can't take a team tool's name, pinned yet or not."""
    taken = next((t.name for t in agent.tools if t.name in MEMBERS), None)
    if taken is not None:
        names = ", ".join(sorted(MEMBERS))
        why = (
            f"tool {taken} of agent {agent.name}: an agent in a team can't have a tool named "
            f"{names}; rename it"
        )
        raise ConfigError("duplicate_name", why)
