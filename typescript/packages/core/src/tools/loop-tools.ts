/**
 * The tools the loop runs itself (loop/framework.ts), with no effect_begin whatever their spec.
 * Rule 17 never lets a tools_changed add one the pin didn't grant.
 */
export const LOOP_TOOLS = [
  "final_output",
  "ask_user",
  "handoff",
  "send_message",
  "spawn_agent",
  "team_task_claim",
  "team_task_create",
  "team_task_update",
  "todo_write",
  "tool_search",
  "send",
  "start",
] as const;
export type LoopTool = (typeof LOOP_TOOLS)[number];

/**
 * The team tools among them: the loop runs them only in a team thread, and elsewhere a user tool
 * may take the name. Lane 21E adds ask, reply, wait, monitor and cancel.
 */
export const TEAM_LOOP_TOOLS: ReadonlySet<LoopTool> = new Set<LoopTool>([
  "send",
  "start",
]);

/** The structured-output tool, pinned with an output model. */
export const FINAL_OUTPUT: LoopTool & "final_output" = "final_output";

const LOOP_TOOL_NAMES: ReadonlySet<string> = new Set(LOOP_TOOLS);

export function isLoopTool(name: string): name is LoopTool {
  return LOOP_TOOL_NAMES.has(name);
}
