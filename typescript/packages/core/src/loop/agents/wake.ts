import type { Fold } from "../../fold/state";
import type { KnownEvent } from "../../log";
import type { EventDraft } from "../../store";
import type { Session } from "../session";
import type { Spawned } from "./spawn";

// A late result recorded while no turn is open, the thread is not cancelled and its log has not
// ended (no member_ended) wakes it: the same append carries woken{causes}, which opens a turn of
// the run that spawned the children and acts for that run's principal (spec/schema/README.md,
// "Background wakes"; rules 32 and 45).

/** Whether a late result recorded now also wakes the thread. */
export function mayWake(
  fold: Pick<Fold, "turnOpen" | "cancelled">,
  events: readonly KnownEvent[],
): boolean {
  return (
    !fold.turnOpen &&
    !fold.cancelled &&
    !events.some((e) => e.type === "member_ended")
  );
}

export function wakeDraft(
  s: Session,
  spawned: Spawned,
  causes: readonly string[],
): EventDraft | undefined {
  if (!mayWake(s.fold, s.events) || causes.length === 0) return undefined;
  const run = s.fold.wake.spawnRuns.get(spawned.data.call_id);
  const opener = s.events.find((e) => e.event_id === run?.root);
  if (opener?.type !== "user_input") return undefined;
  return {
    type: "woken",
    type_version: 1,
    critical: true,
    actor: { kind: "host", principal: opener.actor.principal },
    data: { causes: [...causes] },
  };
}
