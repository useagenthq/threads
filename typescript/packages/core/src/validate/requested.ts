import type { EventOf, Fold } from "../fold/state";
import { invalid, type Violation } from "./violation";

// Rules 29 (output styles) and 30 (requested compaction): operator controls that land only
// between turns, and a request that exactly one outcome answers.

/** Rule 30: one request at a time, between turns, with an input to summarize. */
export function checkCompactionRequested(fold: Fold): Violation {
  if (fold.compactionRequest !== undefined)
    return invalid("a compaction is already requested");
  if (fold.turnOpen)
    return invalid("compaction_requested while a turn is open");
  return fold.firstInput === undefined
    ? invalid("compaction_requested with nothing to compact yet")
    : undefined;
}

/**
 * Rule 30: while a request is unanswered, every compaction step names it; a named request is
 * always the unanswered one.
 */
export function checkCause(fold: Fold, cause: string | undefined): Violation {
  const open = fold.compactionRequest?.event_id;
  if (cause === open) return undefined;
  return open === undefined
    ? invalid(`cause_event_id ${cause} names no unanswered compaction request`)
    : invalid(`a compaction step while ${open} is unanswered must name it`);
}

/** Rule 30 for a compacted: a requested one covers exactly the request's range. */
export function checkRequestedCompacted(
  fold: Fold,
  e: EventOf<"compacted">,
): Violation {
  const wrong = checkCause(fold, e.data.cause_event_id);
  const request = fold.compactionRequest;
  if (wrong !== undefined || request === undefined) return wrong;
  const { trigger, from_seq, to_seq, summary_request_event_id: side } = e.data;
  if (trigger !== "manual")
    return invalid("a compacted answering a request has trigger manual");
  if (from_seq !== fold.firstInput?.seq || to_seq !== request.seq - 1)
    return invalid(
      `a requested compaction covers ${fold.firstInput?.seq}..${request.seq - 1}`,
    );
  const named = side === undefined ? undefined : fold.requests.get(side);
  return named?.cause === request.event_id
    ? undefined
    : invalid("the summary of a requested compaction names the request too");
}

/**
 * Rule 29: an output style is the pinned text, set by an operator between turns or re-appended
 * by the host as the first restore after a compaction that dropped it; never invented, and never
 * the agent's own.
 */
export function checkOutputStyle(
  fold: Fold,
  e: EventOf<"injected">,
): Violation {
  if (e.data.source !== "output_style") return undefined;
  const pinned = fold.policy?.output_styles?.[e.data.origin.id];
  if (e.data.trust !== "trusted_instruction" || e.data.text === undefined)
    return invalid("an output style is inline trusted_instruction text");
  if (pinned !== e.data.text)
    return invalid(`output style ${e.data.origin.id} is not the pinned text`);
  const { kind, principal } = e.actor;
  if (kind === "host")
    return fold.restoreStyle?.data.origin.id === e.data.origin.id
      ? undefined
      : invalid(
          "the host re-appends an output style only right after the compaction that dropped it",
        );
  if (kind !== "user" || principal === undefined)
    return invalid("an output style is set by an operator or the host");
  return fold.turnOpen
    ? invalid("an output style set while a turn is open")
    : undefined;
}
