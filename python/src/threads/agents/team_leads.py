"""The team leads this process defined, by name: open_team resolves a team's agents against the
lead of its name, as a rebind does. ponytail: the latest definition of a name wins; key it by the
lead's config_hash if one process ever defines two leads of one name."""

from dataclasses import dataclass
from typing import Final

from threads.agents.definition import Definition
from threads.agents.teams import Pin, pins
from threads.team.ops import TeamLimits


@dataclass(frozen=True, slots=True)
class Lead:
    pin: Pin
    limits: TeamLimits


_LEADS: Final[dict[str, Lead]] = {}


def register_lead[D](definition: Definition[D]) -> None:
    _LEADS[definition.name] = Lead(pins(definition), definition.team_limits)


def lead_named(name: str) -> Lead | None:
    return _LEADS.get(name)
