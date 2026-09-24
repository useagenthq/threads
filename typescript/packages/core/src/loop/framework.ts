import type { EventOf } from "../fold/state";
import {
  isLoopTool,
  type LoopTool,
  TEAM_LOOP_TOOLS,
} from "../tools/loop-tools";
import { handOff } from "./agents/handoff";
import { sendTool, startTool } from "./agents/members";
import { spawnAgent } from "./agents/spawn";
import { teamTool } from "./agents/team";
import { validateCandidate } from "./output";
import { askUser } from "./questions";
import type { Session } from "./session";
import { writeTodos } from "./todos";
import { searchPinned, searchTools } from "./tool-search";
import type { Halt } from "./types";

// Framework tools: they change only log state, so the loop runs them with its
// session instead of dispatching a ToolImpl. Authorization has already allowed the call.

type Handler = (
  s: Session,
  call: EventOf<"tool_call">,
) => Promise<Halt | undefined> | Halt | undefined;

// Keyed by every LOOP_TOOLS name, so the list and the handlers can't drift apart.
const HANDLERS: Readonly<Record<LoopTool, Handler>> = {
  final_output: validateCandidate,
  todo_write: writeTodos,
  tool_search: searchTools,
  ask_user: askUser,
  spawn_agent: spawnAgent,
  handoff: handOff,
  send_message: teamTool,
  team_task_claim: teamTool,
  team_task_create: teamTool,
  team_task_update: teamTool,
  start: startTool,
  send: sendTool,
};

/**
 * A framework tool's handler; a team tool is one only in a team thread, tool_search only when
 * something is deferred.
 */
export function frameworkTool(s: Session, name: string): Handler | undefined {
  if (!isLoopTool(name)) return undefined;
  if (TEAM_LOOP_TOOLS.has(name) && s.config.team === undefined)
    return undefined;
  if (name === "tool_search" && !searchPinned(s.fold)) return undefined;
  return HANDLERS[name];
}
