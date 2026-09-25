import type { EventOf, Fold, ParkAddress } from "../fold/state";
import type { KnownEvent } from "../log";
import { AskUserInput } from "../tools/agent-inputs";
import { askProblem } from "../tools/ask-user";
import { draft, HOST, TOOL } from "./drafts";
import type { Session } from "./session";
import type { Halt } from "./types";

// ask_user (spec/schema/README.md, "Questions and remembered rules"): a valid question parks the
// branch on {input, call_id} for a day; once that has passed with no answer, the next run on the
// branch records "no answer" and goes on. A host's tick only has to start that run.

const DAY_MS = 86_400_000;

export function askUser(
  s: Session,
  call: EventOf<"tool_call">,
): Promise<Halt | undefined> {
  const { call_id } = call.data;
  // Arguments already parsed before authorization; the question rules are rule 48.
  const problem = askProblem(AskUserInput.parse(call.data.input));
  if (problem !== undefined)
    return s.append(
      draft.toolResult(
        {
          call_id,
          is_error: true,
          origin: "not_executed",
          preview: `invalid input: ${problem}`,
        },
        TOOL,
      ),
    );
  return s.append(
    draft.parked({
      address: { kind: "input", id: call_id },
      reason: "awaiting_input",
      expires_at: s.now() + DAY_MS,
    }),
  );
}

/** The open questions whose expires_at has passed, in the order they were asked. */
export function dueQuestions(
  events: readonly KnownEvent[],
  fold: Fold,
  now: number,
): readonly ParkAddress[] {
  return fold.parked.filter((address) => {
    if (address.kind !== "input") return false;
    const asked = events.findLast(
      (e) =>
        e.type === "parked" &&
        e.data.address.kind === "input" &&
        e.data.address.id === address.id,
    );
    const expires =
      asked?.type === "parked" ? asked.data.expires_at : undefined;
    return expires !== undefined && expires <= now;
  });
}

/** Closes each due question with the error result "no answer", then resumes it. */
export async function expireQuestions(s: Session): Promise<Halt | undefined> {
  for (const address of dueQuestions(s.events, s.fold, s.now())) {
    const closed = await s.append(
      draft.toolResult(
        {
          call_id: address.id,
          is_error: true,
          origin: "not_executed",
          preview: "no answer",
        },
        HOST,
      ),
    );
    if (closed !== undefined) return closed;
    const result = s.events.at(-1);
    if (result === undefined) throw new Error("the result was just appended");
    const stopped = await s.append({
      type: "resumed",
      type_version: 1,
      critical: true,
      actor: HOST,
      data: { address, cause_event_id: result.event_id },
    });
    if (stopped !== undefined) return stopped;
  }
  return undefined;
}
