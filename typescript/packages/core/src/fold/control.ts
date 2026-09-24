import { assertNever } from "../assert-never";
import { type EventOf, type Fold, sameAddress } from "./state";

// Parks, cancels, snapshots, forks and compaction: the control events' fold steps.

export type ControlEvent = EventOf<
  | "parked"
  | "resumed"
  | "cancel_requested"
  | "cancelled"
  | "snapshot"
  | "fork"
  | "compacted"
  | "compaction_failed"
  | "compaction_requested"
>;

export function applyControl(fold: Fold, e: ControlEvent): void {
  switch (e.type) {
    case "parked":
      fold.parked.push(e.data.address);
      return;
    case "resumed": {
      const at = fold.parked.findIndex((a) => sameAddress(a, e.data.address));
      if (at !== -1) fold.parked.splice(at, 1);
      return;
    }
    case "cancel_requested":
      fold.cancelScopes.set(e.event_id, e.data.scope);
      for (const call of fold.calls.values()) call.barrier = true;
      return;
    case "cancelled": {
      const scope = fold.cancelScopes.get(e.data.request_event_id);
      fold.cancelled ||= scope === "thread" || scope === "tree";
      return;
    }
    case "snapshot":
      fold.snapshots.push({
        seq: e.seq,
        eventId: e.event_id,
        quiescent: isQuiescent(fold),
        expiresAt: e.data.expires_at,
      });
      return;
    case "fork":
      // The last fork on the resolved chain is this branch's own.
      fold.repair = e.data.reason === "repair";
      return;
    case "compacted":
      fold.ranges.push([e.data.from_seq, e.data.to_seq]);
      fold.compactionFailures = 0;
      fold.restoreStyle = dropped(fold.outputStyle, e)
        ? fold.outputStyle
        : undefined;
      answer(fold, e.data.cause_event_id);
      return;
    case "compaction_failed":
      fold.compactionFailures += 1;
      answer(fold, e.data.cause_event_id);
      return;
    case "compaction_requested":
      fold.compactionRequest = e;
      return;
    default:
      assertNever(e);
  }
}

function dropped(
  style: EventOf<"injected"> | undefined,
  compacted: EventOf<"compacted">,
): boolean {
  const { from_seq, to_seq } = compacted.data;
  return style !== undefined && style.seq >= from_seq && style.seq <= to_seq;
}

/** An outcome naming the unanswered compaction request answers it (rule 30 checked the name). */
function answer(fold: Fold, cause: string | undefined): void {
  if (cause !== undefined && cause === fold.compactionRequest?.event_id)
    fold.compactionRequest = undefined;
}

/** C4 without the expiry, which depends on the reader's clock. */
function isQuiescent(fold: Fold): boolean {
  const settled = [...fold.effects.values()].every(
    (effect) => effect.status === "committed" || effect.status === "resolved",
  );
  return (
    !fold.turnOpen &&
    fold.pending.size === 0 &&
    fold.parked.length === 0 &&
    settled
  );
}
