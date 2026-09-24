"""Whether a case reruns offline from its directory alone (spec lane 22, A.2 and A.4): the reason
save_case recorded, or what an older case's files can't answer. The runner skips such a case with
`offline_not_runnable:<reason>`."""

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Final

from pydantic import JsonValue

from threads.log import ToolSpec
from threads.loop.output import FINAL_OUTPUT
from threads.tools.specs import FRAMEWORK, MEMBERS, TEAM

if TYPE_CHECKING:
    from threads.evals.case_dir import CaseDir

FRAMEWORK_TOOLS: Final = FRAMEWORK | {FINAL_OUTPUT}
"""The tools the loop runs from the log itself: a saved case never records their results."""
CHILD_TOOLS: Final = frozenset({"spawn_agent", "handoff", "start"})
"""Tools that start or hand to another thread: a child whose model calls the case lacks."""
TEAM_TOOLS: Final = TEAM | MEMBERS
"""The team model tools: they act on member threads the rerun doesn't have."""


def tool_uses(model: JsonValue) -> tuple[str, ...]:
    """The tool names the recorded model replies call, in order."""
    responses = model.get("responses") if isinstance(model, dict) else None
    if not isinstance(responses, list):
        return ()
    names: list[str] = []
    for response in responses:
        content = response.get("content") if isinstance(response, dict) else None
        for part in content if isinstance(content, list) else []:
            if isinstance(part, dict) and part.get("type") == "tool_use":
                name = part.get("name")
                if isinstance(name, str):
                    names.append(name)
    return tuple(names)


def _without_results(uses: Sequence[str], tools: Mapping[str, ToolSpec]) -> str | None:
    """A case with no sandbox.json (a Python save_case before this lane): runnable only when the
    turn made no read-only call that needs a recorded result. A framework tool never does; a name
    the tool set in effect doesn't know does."""
    for name in uses:
        if name in FRAMEWORK_TOOLS:
            continue
        spec = tools.get(name)
        if spec is None or spec.effect_class == "read_only":
            return "resave_case"
    return None


def offline_block(case: "CaseDir", tools: Mapping[str, ToolSpec]) -> str | None:
    """The reason `case` can't rerun offline, or None. `tools`: the set in effect at the turn."""
    if case.meta.offline is not None:
        return case.meta.offline.reason
    uses = tool_uses(case.model)
    if any(u in CHILD_TOOLS for u in uses):
        return "child_threads"
    if any(u in TEAM_TOOLS for u in uses):
        return "team_calls"
    sandbox = case.sandbox
    if sandbox is None:
        return _without_results(uses, tools)
    if sandbox.v1 is None:
        return None
    once = all(uses.count(name) == 1 for name in sandbox.v1)
    return None if once else "resave_case"
