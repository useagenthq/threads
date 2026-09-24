"""Names of the tools the loop runs itself. A leaf module: the validator (rule 17) and the tool
catalog both read it, and neither can import the other."""

from typing import Final

TEAM: Final = frozenset({"send_message", "team_task_claim", "team_task_create", "team_task_update"})
"""Offered to a team: an agent with subagents, and every child it spawns."""
PINNED_MEMBERS: Final = frozenset({"send", "start"})
"""The team tools pinned and run so far: lane 21E pins ask, reply, wait, monitor and cancel."""
FRAMEWORK: Final = TEAM | PINNED_MEMBERS | {"todo_write", "handoff", "spawn_agent"}
"""Log-only tools: `read_only` is exact, since only log state changes."""
FINAL_OUTPUT: Final = "final_output"
"""The structured-output tool, pinned with an output model."""
SEARCH: Final = "tool_search"
"""Loads deferred tools; pinned only when something is deferred."""
LOOP_TOOLS: Final = FRAMEWORK | {FINAL_OUTPUT, SEARCH}
"""The tools the loop runs itself, with no effect_begin whatever their spec. Rule 17 never lets a
tools_changed add one the pin didn't grant."""
