import type { z } from "zod";
import type { KnownEvent } from "../log";
import { containsSecret, redactStrings } from "../redact";
import { err, ok, type Result } from "../result";
import { addLine, type Chain, type ChainEvent } from "../verify";
import { type LogError, logError } from "../verify/error";
import { canonicalLine, uuidv7 } from "./encode";

type EnvelopeKey =
  | "seq"
  | "event_id"
  | "thread_id"
  | "branch_id"
  | "epoch"
  | "time"
  | "prev_hash";
type DistributiveOmit<T, K extends PropertyKey> = T extends unknown
  ? Omit<T, K>
  : never;
/**
 * An event before the writer fills its envelope. It is parsed like any stored line. A draft may
 * bring its own `event_id` (minted with `uuidv7` from the writer's clock) when a later event of
 * the same append must name it; the line schema checks its format and validate_next that it is
 * unique.
 */
export type EventDraft = DistributiveOmit<
  z.input<typeof KnownEvent>,
  EnvelopeKey
> & { readonly event_id?: string };

/** What admitting a batch left: its events, and the ids of those that opened a turn. */
export type Admitted = {
  readonly events: readonly ChainEvent[];
  readonly opened: ReadonlySet<string>;
};

/**
 * Admits `drafts` onto `trial` in order, each through the same checks as an import (C5
 * redaction, the line schema, validate_next): what a writer's append and `branch.open` store.
 */
export function admitDrafts(
  trial: Chain,
  drafts: readonly EventDraft[],
  now: number,
  epoch: number,
): Result<Admitted, LogError> {
  const events: ChainEvent[] = [];
  const opened = new Set<string>();
  for (const draft of drafts) {
    const before = trial.fold.turnOpen;
    const event = admitDraft(trial, draft, now, epoch);
    if (!event.ok) return event;
    events.push(event.value);
    if (!before && trial.fold.turnOpen) opened.add(event.value.event.event_id);
  }
  return ok({ events, opened });
}

function admitDraft(
  trial: Chain,
  draft: EventDraft,
  now: number,
  epoch: number,
): Result<ChainEvent, LogError> {
  const segment = trial.segments.at(-1);
  if (segment === undefined) throw new Error("a writer's chain has a header");
  // Nothing is recorded with a resolved secret in it (C5): every event passes here.
  const actor = redactStrings(draft.actor);
  const data = redactStrings(draft.data);
  const content = canonicalLine({ actor, data });
  if (!content.ok) return content;
  // Canonical escaping or JSON punctuation can still join redacted strings into a value.
  if (containsSecret(content.value))
    return err(
      logError(
        "secret_in_stored_bytes",
        "the event's stored bytes would hold a registered secret; nothing appended",
      ),
    );
  const line = canonicalLine({
    ...draft,
    actor,
    data,
    seq: trial.fold.seq + 1,
    event_id: draft.event_id ?? uuidv7(now),
    thread_id: segment.header.thread_id,
    branch_id: segment.header.branch_id,
    epoch,
    time: now,
    prev_hash: segment.events.at(-1)?.hash ?? segment.hash,
  });
  if (!line.ok) return line;
  const admitted = addLine(trial, line.value);
  if (!admitted.ok) return admitted;
  const event = trial.events.at(-1);
  if (event === undefined) throw new Error("an admitted event is on the chain");
  return ok(event);
}

/** A trial copy of a chain: new arrays and fold, shared immutable events. */
// ponytail: O(chain length) per append batch; keep an undo log instead if batches get hot.
export function copyChain(chain: Chain): Chain {
  return {
    segments: chain.segments.map((s) => ({ ...s, events: [...s.events] })),
    events: [...chain.events],
    fold: structuredClone(chain.fold),
  };
}
