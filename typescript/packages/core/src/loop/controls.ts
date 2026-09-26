import { EventId } from "../log";
import { knownEvents } from "../reduce";
import { err, ok } from "../result";
import type { EventDraft } from "../store";
import { uuidv7 } from "../store/encode";
import { isRefusal } from "../store/writer";
import {
  consumeControlItems,
  pendingControlItems,
} from "../thread/control-items";
import { cancel, stopWhenIdle } from "../thread/settings";
import type { Session } from "./session";
import { BARRED, type Halt } from "./types";

// The holder's side of a cross-process cancel (lane 29F). The lease holder applies the thread's
// durable control items at its own step boundaries, in the same transaction that consumes them,
// so a cancel another process asked for lands before this run's next dispatch. A run that takes
// a free lease does it on its first boundary too, which is how a cancel left behind by a crash
// is applied before anything else. `consumed_seq IS NULL` is the CAS: exactly one owner applies
// each item, and a loser's whole append rolls back.

/** Applies every control item waiting for this thread, once. Undefined: none, or nothing to do. */
export async function takeControlItems(s: Session): Promise<Halt | undefined> {
  if ((await s.config.controlItems?.()) !== true) return undefined;
  const appended = await s.appendDecided(async (tx) => {
    const items = await pendingControlItems(tx.tx, s.threadId);
    const drafts: EventDraft[] = [];
    for (const item of items) {
      const plan = (item.command === "cancel" ? cancel : stopWhenIdle)(
        item.principal,
      )(knownEvents(tx.chain), tx.chain);
      // Neither plan refuses; one that did would be a bug, and applying nothing is the safe half.
      if (!plan.ok) continue;
      const id = EventId.parse(uuidv7(tx.now));
      drafts.push(
        { ...plan.value.record, event_id: id },
        ...(plan.value.after?.(id) ?? []),
      );
    }
    if (drafts.length === 0) return ok([]);
    // Consumed at this append's first event, inside the append's own transaction.
    const taken = await consumeControlItems(
      tx.tx,
      items,
      tx.chain.fold.seq + 1,
    );
    return taken
      ? ok(drafts)
      : err({
          code: "consumed",
          message: "a control item is already consumed",
        });
  });
  if (appended === BARRED || isRefusal(appended)) return undefined;
  return appended;
}
