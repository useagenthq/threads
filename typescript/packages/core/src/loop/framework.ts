import type { EventOf } from "../fold/state";
import { spawnAgent } from "./agents/spawn";
import { teamTool } from "./agents/team";
import { FINAL_OUTPUT, validateCandidate } from "./output";
import type { Session } from "./session";
import { writeTodos } from "./todos";
import type { Halt } from "./types";

// Framework tools: they change only log state, so the loop runs them with its
// session instead of dispatching a ToolImpl. Authorization has already allowed the call.

type Handler = (
  s: Session,
  call: EventOf<"tool_call">,
) => Promise<Halt | undefined> | Halt | undefined;

const HANDLERS: ReadonlyMap<string, Handler> = new Map<string, Handler>([
  [FINAL_OUTPUT, validateCandidate],
  ["todo_write", writeTodos],
  ["spawn_agent", spawnAgent],
  ["send_message", teamTool],
  ["team_task_claim", teamTool],
  ["team_task_create", teamTool],
  ["team_task_update", teamTool],
]);

export function frameworkTool(name: string): Handler | undefined {
  return HANDLERS.get(name);
}
