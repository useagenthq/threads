import type { z } from "zod";
import type { EventOf } from "../fold/state";
import { canonicalize } from "../log";
import { draft, TOOL } from "./drafts";
import { endTurn } from "./request";
import { parseErrors } from "./schema";
import type { Session } from "./session";
import { turnEvents } from "./turn";
import type { Halt } from "./types";

// Structured final output in tool mode: each final_output candidate is
// checked once and recorded as output_validated, next to its raw tool_call.

export const FINAL_OUTPUT = "final_output";
const ASK_FOR_OUTPUT = `Return the final result by calling ${FINAL_OUTPUT}.`;

export function validateCandidate(
  s: Session,
  call: EventOf<"tool_call">,
): Halt | undefined {
  const output = s.fold.policy?.output;
  if (output === undefined)
    return s.append(
      draft.toolResult(
        {
          call_id: call.data.call_id,
          is_error: true,
          origin: "not_executed",
          preview: "no output schema is pinned",
        },
        { kind: "host" },
      ),
    );
  // The agent's own schema validates; a pinned schema without one fails closed.
  const schema = s.config.output;
  if (schema === undefined)
    return {
      code: "output_invalid",
      message: "no validator for the pinned output schema",
    };
  const errors = parseErrors(schema, call.data.input);
  const common = {
    source_event_id: call.event_id,
    schema_sha256: output.schema_sha256,
  };
  if (errors === undefined) {
    const value = call.data.input;
    const shown = canonicalize(value);
    return (
      s.append(
        draft.outputValidated({ ...common, outcome: "accepted", value }),
      ) ??
      s.append(
        draft.toolResult(
          {
            call_id: call.data.call_id,
            is_error: false,
            origin: "executed",
            preview: shown.ok ? shown.value : "",
          },
          TOOL,
        ),
      )
    );
  }
  const stopped =
    s.append(
      draft.outputValidated({
        ...common,
        outcome: "rejected",
        errors: [{ path: "", message: errors }],
      }),
    ) ??
    s.append(
      draft.toolResult(
        {
          call_id: call.data.call_id,
          is_error: true,
          origin: "not_executed",
          preview: `final_output rejected: ${errors}`,
        },
        TOOL,
      ),
    );
  return stopped ?? giveUp(s, output.max_retries);
}

/** After max_retries failed candidates the turn ends output_invalid. */
function giveUp(s: Session, maxRetries: number): Halt | undefined {
  const turn = turnEvents(s.events);
  const failures =
    turn.filter(
      (e) => e.type === "output_validated" && e.data.outcome === "rejected",
    ).length +
    turn.filter(
      (e) => e.type === "injected" && e.data.origin.id === FINAL_OUTPUT,
    ).length;
  return failures >= maxRetries ? endTurn(s, "output_invalid") : undefined;
}

/**
 * A turn that ends in plain text while an output schema is pinned counts as a failed
 * candidate: the model is asked for final_output, or the turn ends output_invalid.
 */
export function missingCandidate(s: Session): Halt | undefined {
  const output = s.fold.policy?.output;
  if (output === undefined) return endTurn(s, "end_turn");
  const stopped = s.append(
    draft.injected({
      source: "recovery",
      trust: "trusted_instruction",
      origin: { id: FINAL_OUTPUT },
      text: ASK_FOR_OUTPUT,
    }),
  );
  return stopped ?? giveUp(s, output.max_retries);
}

/** The accepted value of this turn, if any. */
export function acceptedValue(s: Session): z.core.util.JSONType | undefined {
  const accepted = turnEvents(s.events).findLast(
    (e) => e.type === "output_validated" && e.data.outcome === "accepted",
  );
  return accepted?.type === "output_validated" &&
    accepted.data.outcome === "accepted"
    ? accepted.data.value
    : undefined;
}
