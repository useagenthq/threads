"""The team leads this process defined, by name, each held weakly by its agent handle: open_team
rebinds a team's lead by its name and config_hash among them, as materialize rebinds a member."""

import weakref
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass, replace
from typing import Final

from threads.agents.definition import Definition
from threads.agents.pinned import outside_any_branch
from threads.agents.servers import with_servers
from threads.agents.setup import set_up
from threads.agents.teams import Pin, pins
from threads.log.digest import sha256_hex
from threads.team.ops import TeamLimits


@dataclass(frozen=True, slots=True)
class Lead:
    name: str
    pin: Pin
    limits: TeamLimits
    config_hash: Callable[[bool, bool], Awaitable[str]]
    """Its config_hash as a thread of its own (member, answerer): a nested lead's pinned as a
    member, a hosted one's with ask_user. Raises ConfigError."""


# The handle is the key: a lead nobody holds any more is dropped with it.
_LEADS: Final[weakref.WeakKeyDictionary[object, Lead]] = weakref.WeakKeyDictionary()


def register_lead[D](handle: object, definition: Definition[D]) -> None:
    async def config_hash(member: bool, answerer: bool) -> str:
        await set_up(definition)
        async with AsyncExitStack() as stack:
            connected = await with_servers(definition, stack, outside_any_branch)
            _, config = replace(connected, in_team=member, answerer=answerer).pin()
        return sha256_hex(config)

    lead = Lead(definition.name, pins(definition), definition.team_limits, config_hash)
    _LEADS[handle] = lead


def leads_named(name: str) -> tuple[Lead, ...]:
    """Every live lead of that name."""
    return tuple(lead for lead in tuple(_LEADS.values()) if lead.name == name)
