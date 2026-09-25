"""Rebinding a member's definition by name in this process (design §4.10, prework): a dynamic
member's from its template with the recorded define and starter. Only a definition not registered
here (pin_unavailable) or a different config_hash (pin_mismatch) fails it; an error setting the
definition up is for now ("later"). Mirrors TypeScript's TeamWorker #rebind."""

from collections.abc import Awaitable, Callable, Mapping
from typing import Literal

from pydantic.experimental.missing_sentinel import MISSING

from threads.agents.config import ConfigError, UnboundError
from threads.agents.definition import Definition
from threads.agents.dynamic_agent import member_definition
from threads.agents.store import now_ms
from threads.log import MailEnvelope, MemberRef, MemberStartedEvent
from threads.loop.team_runtime import TeamAgentPin
from threads.store.lines import uuid7
from threads.team.dynamic import OPERATOR
from threads.team.materialize import Rebind


def bound(
    agents: Mapping[str, Definition[None]], started: MemberStartedEvent, task: MailEnvelope
) -> Definition[None] | None:
    """The definition a member runs, rebound by its agent's name; None when it is gone."""
    found = agents.get(started.data.agent)
    define = started.data.define
    if found is None or define is MISSING:
        return found
    # A caller never sends a task (the envelope's rule), so a nameless sender is the operator.
    sender = task.from_
    starter = sender.name if isinstance(sender, MemberRef) else OPERATOR
    try:
        return member_definition(found, define, starter)
    except ConfigError:
        return None


async def rebind(
    agents: Mapping[str, Definition[None]],
    pin: Callable[[Definition[None]], Awaitable[TeamAgentPin]],
    started: MemberStartedEvent,
    task: MailEnvelope,
) -> Rebind | Literal["later"]:
    """The member's rebind, or "later" when its setup failed for now."""
    found = bound(agents, started, task)
    if found is None:
        return Rebind("pin_unavailable")
    try:
        pinned = await pin(found)
    except UnboundError:
        return Rebind("pin_unavailable")
    except ConfigError:
        return "later"
    if pinned.config_hash != started.data.config_hash:
        return Rebind("pin_mismatch")
    if found.team is None:
        return Rebind("ok")
    now = now_ms()
    team = {"id": uuid7(now), "log_thread_id": uuid7(now), "log_branch_id": uuid7(now)}
    return Rebind("ok", team)
