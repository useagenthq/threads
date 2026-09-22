import type { EventOf, Fold } from "../fold/state";
import { sha256Hex } from "../hash";
import { invalid, type Violation } from "./violation";

// Rules 10, 12, 18, 21 and 26.

/** Rules 12 and 26: input opens a turn only when none is open, and never after a handoff. */
export function checkInput(
  fold: Fold,
  e: EventOf<"user_input" | "steer">,
): Violation {
  if (fold.handedOff) return invalid(`${e.type} after handoff`);
  return e.type === "user_input" && fold.turnOpen
    ? invalid("user_input while a turn is open (use steer)")
    : undefined;
}

/** Rules 21 and 26. */
export function checkModelRequest(fold: Fold): Violation {
  if (fold.handedOff) return invalid("model_request after handoff");
  return fold.budgetBlocked
    ? invalid("model_request after budget_exceeded without new input")
    : undefined;
}

/** Rule 18: never during an attempt, and only to a priced model. */
export function checkSettings(
  fold: Fold,
  e: EventOf<"settings_changed">,
): Violation {
  if (fold.awaiting.size > 0)
    return invalid(
      "settings_changed while a model attempt awaits its response",
    );
  const models = fold.policy?.models;
  const { provider, name } = e.data.settings.model;
  const listed = models?.some(
    (m) => m.provider === provider && m.name === name,
  );
  return listed === false
    ? invalid(
        `settings_changed to ${provider}/${name}, which policy.models does not list`,
      )
    : undefined;
}

/** Rule 10: step-boundary edges, nested or disjoint ranges, and the recorded summary. */
export function checkCompacted(fold: Fold, e: EventOf<"compacted">): Violation {
  const { from_seq: from, to_seq: to } = e.data;
  if (from > to || to >= e.seq)
    return invalid(`compacted range ${from}..${to} is not before the event`);
  if (fold.boundaries[from - 1] !== true || fold.boundaries[to] !== true)
    return invalid(`compacted range ${from}..${to} splits a step`);
  const partial = fold.ranges.some(
    ([a, b]) =>
      !(to < a || from > b) &&
      !(from <= a && b <= to) &&
      !(a <= from && to <= b),
  );
  if (partial) return invalid(`compacted range ${from}..${to} partly overlaps`);
  return checkSummary(fold, e);
}

function checkSummary(fold: Fold, e: EventOf<"compacted">): Violation {
  const requestId = e.data.summary_request_event_id;
  if (requestId === undefined) return undefined;
  const text = fold.summaries.get(requestId);
  const ref = e.data.summary_ref;
  const matches =
    text !== undefined &&
    sha256Hex(text) === ref.sha256 &&
    new TextEncoder().encode(text).length === ref.bytes;
  return matches
    ? undefined
    : invalid("summary_ref is not the text of its compaction response");
}
