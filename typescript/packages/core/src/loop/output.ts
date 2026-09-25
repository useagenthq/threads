import type { EventOf } from "../fold/state";
import { canonicalize } from "../log";
import { FINAL_OUTPUT } from "../tools/loop-tools";
import { conforms } from "../validate/json-schema";
import { draft, TOOL } from "./drafts";
import { endTurn } from "./request";
import { parseErrors } from "./schema";
import type { Session } from "./session";
import { turnEvents } from "./turn";
import type { Halt } from "./types";

// Structured final output in tool mode: each final_output candidate is
// checked once and recorded as output_validated, next to its raw tool_call.

const ASK_FOR_OUTPUT = `Return the final result by calling ${FINAL_OUTPUT}.`;

export async function validateCandidate(
  s: Session,
  call: EventOf<"tool_call">,
): Promise<Halt | undefined> {
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
  // The log's own check (semantic rule 20) must pass too, whatever the agent's schema lets
  // through: an accepted value never fails the reader.
  const errors =
    parseErrors(schema, call.data.input) ??
    (conforms(output.schema, call.data.input)
      ? undefined
      : "the value fails the pinned output schema (its formats and bounds)");
  const common = {
    source_event_id: call.event_id,
    schema_sha256: output.schema_sha256,
  };
  if (errors === undefined) {
    const value = call.data.input;
    const shown = canonicalize(value);
    return (
      (await s.append(
        draft.outputValidated({ ...common, outcome: "accepted", value }),
      )) ??
      (await s.append(
        draft.toolResult(
          {
            call_id: call.data.call_id,
            is_error: false,
            origin: "executed",
            preview: shown.ok ? shown.value : "",
          },
          TOOL,
        ),
      ))
    );
  }
  const stopped =
    (await s.append(
      draft.outputValidated({
        ...common,
        outcome: "rejected",
        errors: [{ path: "", message: errors }],
      }),
    )) ??
    (await s.append(
      draft.toolResult(
        {
          call_id: call.data.call_id,
          is_error: true,
          origin: "not_executed",
          preview: `final_output rejected: ${errors}`,
        },
        TOOL,
      ),
    ));
  return stopped ?? (await giveUp(s, output.max_retries));
}

/** After max_retries failed candidates the turn ends output_invalid. */
async function giveUp(
  s: Session,
  maxRetries: number,
): Promise<Halt | undefined> {
  const turn = turnEvents(s.events, s.fold);
  const failures =
    turn.filter(
      (e) => e.type === "output_validated" && e.data.outcome === "rejected",
    ).length +
    turn.filter(
      (e) => e.type === "injected" && e.data.origin.id === FINAL_OUTPUT,
    ).length;
  return failures >= maxRetries
    ? await endTurn(s, "output_invalid")
    : undefined;
}

/**
 * A turn that ends in plain text while an output schema is pinned counts as a failed
 * candidate: the model is asked for final_output, or the turn ends output_invalid.
 */
export async function missingCandidate(s: Session): Promise<Halt | undefined> {
  const output = s.fold.policy?.output;
  if (output === undefined) return endTurn(s, "end_turn");
  const stopped = await s.append(
    draft.injected({
      source: "recovery",
      trust: "trusted_instruction",
      origin: { id: FINAL_OUTPUT },
      text: ASK_FOR_OUTPUT,
    }),
  );
  return stopped ?? (await giveUp(s, output.max_retries));
}
