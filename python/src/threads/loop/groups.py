"""Parallel tool calls (spec/schema/README.md): which pending calls may run together. Pinned by
spec/conformance/vectors/tool-groups.json."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from threads.log import EffectClass

type Decision = Literal["allow", "ask", "deny", "none"]
"""A call's recorded authorization; none: not authorized yet."""

type Plan = tuple[tuple[int, ...], ...]
"""Call indexes by group; a group of one runs alone."""


@dataclass(frozen=True, slots=True)
class Candidate:
    """What the grouping rule reads about one pending call."""

    concurrent: bool
    """The bound tool is declared `concurrent=True`."""
    effect_class: EffectClass
    """The pinned effect class."""
    framework: bool
    ends_turn: bool
    decision: Decision


def joins(c: Candidate) -> bool:
    """Whether this call may run in a group."""
    return (
        c.concurrent
        and c.effect_class == "read_only"
        and not c.framework
        and not c.ends_turn
        and c.decision == "allow"
    )


def groups(pending: Sequence[Candidate]) -> Plan:
    """The dispatch plan: each group is the longest run of consecutive calls that may run
    together; every other call is a group of one."""
    plan: list[list[int]] = []
    for i, c in enumerate(pending):
        if plan and joins(pending[plan[-1][0]]) and joins(c):
            plan[-1].append(i)
        else:
            plan.append([i])
    return tuple(tuple(g) for g in plan)
