import type { EventOf } from "../fold/state";
import { handOff } from "./agents/handoff";
import { sendTool, startTool } from "./agents/members";
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
  ["handoff", handOff],
  ["send_message", teamTool],
  ["team_task_claim", teamTool],
  ["team_task_create", teamTool],
  ["team_task_update", teamTool],
  ["start", startTool],
  ["send", sendTool],
]);

const TEAM_HANDLERS: ReadonlySet<string> = new Set(["start", "send"]);

/** A framework tool's handler; a team tool is one only in a team thread. */
export function frameworkTool(s: Session, name: string): Handler | undefined {
  if (TEAM_HANDLERS.has(name) && s.config.team === undefined) return undefined;
  return HANDLERS.get(name);
}
