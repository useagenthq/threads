"""What a start pins before its append (design §4.10, before the transaction), for the lead's start
tool and the operator's team.start alike: the named agent's pin, or a dynamic agent's member as the
start's chosen fields define it, with its config bytes stored."""

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from threads.loop.team_runtime import TeamAgentPin
from threads.result import Ok
from threads.team.dynamic import Choice, InvalidDefinition, Resolved, resolve_definition


@dataclass(frozen=True, slots=True)
class Chosen:
    """A start's fields besides its agent and task."""

    label: str | None = None
    instructions: str | None = None
    tools: Sequence[str] | None = None
    model: str | None = None


@dataclass(frozen=True, slots=True)
class StartPin:
    pinned: TeamAgentPin | None
    """The member's pin; None when the team lists no such agent."""
    resolved: Resolved | InvalidDefinition


async def start_pin(
    pin: Callable[[str, Choice | None], Awaitable[TeamAgentPin | None]],
    put: Callable[[bytes], Awaitable[object]],
    agent: str,
    chosen: Chosen,
    starter: str,
) -> StartPin:
    """`starter` is the name the block says wrote it: the starting lead's, or `operator`."""
    pinned = await pin(agent, None)
    if pinned is None:
        return StartPin(None, Resolved())
    got = resolve_definition(
        pinned.template,
        label=chosen.label,
        instructions=chosen.instructions,
        tools=None if chosen.tools is None else tuple(chosen.tools),
        model=chosen.model,
    )
    resolved = got.value if isinstance(got, Ok) else got.error
    if isinstance(resolved, Resolved) and resolved.define is not None:
        pinned = await pin(agent, Choice(resolved.define, starter))
    if pinned is not None:
        for raw in (pinned.config, *pinned.specs):
            await put(raw)
    return StartPin(pinned, resolved)
