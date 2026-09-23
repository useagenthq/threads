import type { EventOf } from "../fold/state";
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
]);

export function frameworkTool(name: string): Handler | undefined {
  return HANDLERS.get(name);
}
