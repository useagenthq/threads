import type { EventOf, ParkAddress } from "../../fold/state";
import type { Session } from "../session";
import type { Halt } from "../types";
import { runChild, type Spawned } from "./spawn";

// A child that parks parks its parent (spec/schema/README.md, "Subagent cancellation and
// parking"): no agent_finished, a parked{kind: child} in the parent, and the spawn call stays
// pending. The parent's next run re-runs each child it waits on and resumes once one no longer
// parks; the call's re-dispatch then records the child's end.

type Reason = EventOf<"parked">["data"]["reason"];

function addressOf(spawned: Spawned): ParkAddress {
  return { kind: "child", id: spawned.data.child_thread_id };
}

function parkedOn(s: Session, address: ParkAddress): boolean {
  return s.fold.parked.some(
    (a) => a.kind === address.kind && a.id === address.id,
  );
}

export function parkOn(
  s: Session,
  spawned: Spawned,
  reason: Reason,
): Halt | undefined {
  const address = addressOf(spawned);
  if (parkedOn(s, address)) return undefined;
  return s.append({
    type: "parked",
    type_version: 1,
    critical: true,
    actor: { kind: "host" },
    data: { address, reason },
  });
}

/** Resumes every child park whose child no longer ends parked. */
export async function unparkChildren(s: Session): Promise<Halt | undefined> {
  for (const address of [...s.fold.parked]) {
    if (address.kind !== "child") continue;
    const spawned = s.events.find(
      (e): e is Spawned =>
        e.type === "agent_spawned" && e.data.child_thread_id === address.id,
    );
    if (spawned === undefined) continue;
    const end = await runChild(s, spawned);
    // Still parked, or not runnable right now: this thread stays parked on it.
    if ("code" in end || end.status === "parked") continue;
    const stopped = s.append({
      type: "resumed",
      type_version: 1,
      critical: true,
      actor: { kind: "host" },
      data: { address, cause_event_id: spawned.event_id },
    });
    if (stopped !== undefined) return stopped;
  }
  return undefined;
}
