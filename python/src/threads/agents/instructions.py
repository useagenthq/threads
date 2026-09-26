"""Line 0's instructions (Render v1): pinned per thread, so every part here is a pure function of
the agent's definition."""

from dataclasses import replace
from typing import TYPE_CHECKING

from threads.agents.skills import listing
from threads.team.dynamic import KEPT, block
from threads.team.policy import startable

if TYPE_CHECKING:
    from threads.agents.definition import Definition


def full_instructions[D](d: "Definition[D]") -> str:
    """Base instructions, then each extension's, in declaration order, then the skill listing,
    then the agents this one may start, hand off to and start as team members: all line 0, so
    pinned per thread (C7). A host rule that allows start adds its target to the team listing."""
    parts = [d.instructions, *(e.instructions for e in d.extensions)]
    parts.append(listing(d.skills))
    if d.subagents:
        parts.append(f"Subagents you can start with spawn_agent: {_names(d.subagents)}.")
    if d.handoffs:
        parts.append(f"Agents you can hand the conversation to: {_names(d.handoffs)}.")
    team = startable(d.team, d.rules, d.rule_agents)
    if team:
        listed = [f"Agents you can start as team members with start: {_names(team)}."]
        listed += [_template_line(a) for a in team if a.models]
        parts.append("\n".join(listed))
    written = None if d.dynamic is None else d.dynamic.define.instructions
    if d.dynamic is not None and isinstance(written, str):
        parts.append(block(d.dynamic.starter, written))
    return "\n\n".join(p for p in parts if p)


def _names[D](agents: "tuple[Definition[D], ...]") -> str:
    return ", ".join(a.name for a in agents)


def choosable[D](template: "Definition[D]") -> tuple[str, ...]:
    """A template's tools a start may choose: its member pin's names but F, in pinned order."""
    specs = replace(template, in_team=True).specs()
    return tuple(s.name for s in specs if s.name not in KEPT)


def _template_line[D](template: "Definition[D]") -> str:
    """The lead's listing line for a dynamic agent (spec/schema/README.md, Dynamic members)."""
    tools = ", ".join(choosable(template)) or "none"
    keys = [k for k, _ in template.models]
    models = ", ".join([f"{keys[0]} (default)", *keys[1:]])
    return f"{template.name} (you write its instructions; tools: {tools}; models: {models})"
