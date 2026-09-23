import { AGENT_TOOLS, entry } from "../tools/catalog";
import { authorize } from "./authorize";
import { draft } from "./drafts";
import { FINAL_OUTPUT } from "./output";
import { parseErrors } from "./schema";
import type { Session } from "./session";
import type { Response } from "./turn";
import { toolSpec } from "./turn";
import type { Halt } from "./types";

// A response's tool calls become durable before anything runs: tool_call (the parsed args),
// then its authorization, or an error result for a call that fails before any effect.

type ToolUse = Extract<
  Response["data"]["content"][number],
  { type: "tool_use" }
>;

export async function recordCalls(
  s: Session,
  response: Response,
): Promise<Halt | undefined> {
  for (const use of response.data.content) {
    if (use.type !== "tool_use") continue;
    const stopped = await recordCall(s, use, response.data.request_event_id);
    if (stopped !== undefined) return stopped;
  }
  return undefined;
}

async function recordCall(
  s: Session,
  use: ToolUse,
  requestEventId: string,
): Promise<Halt | undefined> {
  const { call_id, name, input } = use;
  const stopped = s.append(
    draft.toolCall({ call_id, name, input, request_event_id: requestEventId }),
  );
  if (stopped !== undefined) return stopped;
  const failure = preEffectFailure(s, use);
  if (failure !== undefined)
    return s.append(
      draft.toolResult(
        { call_id, is_error: true, origin: "not_executed", preview: failure },
        { kind: "host" },
      ),
    );
  const call = s.events.at(-1);
  if (call?.type !== "tool_call")
    throw new Error("the tool_call was just appended");
  return authorize(s, call);
}

/** Unknown, deferred or invalid calls fail before any effect, with a reason the model sees. */
function preEffectFailure(s: Session, use: ToolUse): string | undefined {
  const spec = toolSpec(s.fold, use.name);
  if (spec === undefined) return `unknown tool: ${use.name}`;
  // A final_output candidate is checked once, as output_validated.
  if (use.name === FINAL_OUTPUT) return undefined;
  if (spec.defer_loading === true)
    return `tool_not_loaded: ${use.name}; find it with tool_search first`;
  const input = AGENT_TOOLS.has(use.name)
    ? entry(use.name).input
    : s.config.tools.get(use.name)?.input;
  // Arguments parse with the tool's own schema; a tool without one fails closed.
  if (input === undefined) return `no implementation for ${use.name}`;
  const errors = parseErrors(input, use.input);
  return errors === undefined ? undefined : `invalid input: ${errors}`;
}
