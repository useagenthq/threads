import type { Session } from "../session";
import type { Halt } from "../types";
import { stopGate } from "./gates";
import { finish, finished, launch, spawnedFor } from "./spawn";

// Background children: each runs concurrently in this process and
// hands its end to the parent's loop, which records it at a step boundary, so the parent's
// single writer never interleaves a late result into a step.

/** Records every child that ended since the last step: subagent_stop, then its late result. */
export async function settleBackground(s: Session): Promise<Halt | undefined> {
  for (const [callId, end] of [...s.finished]) {
    s.finished.delete(callId);
    s.background.delete(callId);
    const spawned = spawnedFor(s, callId);
    if (spawned === undefined)
      throw new Error("a background child was spawned");
    const next = await stopGate(s, spawned, finished(s, spawned, end));
    if (next === "continue") launch(s, spawned);
    else {
      const stopped = next === "stop" ? finish(s, spawned, end, true) : next;
      if (stopped !== undefined) return stopped;
    }
  }
  return undefined;
}

/** Before a run returns: every running child ends and is recorded while this writer holds the lease. */
export async function drainBackground(s: Session): Promise<Halt | undefined> {
  while (s.background.size > 0 || s.finished.size > 0) {
    if (s.finished.size === 0) await Promise.race(s.background.values());
    const stopped = await settleBackground(s);
    if (stopped !== undefined) return stopped;
  }
  return undefined;
}

/** After a restart: background children with no agent_finished run again (resumed, not re-sent). */
export function resumeBackground(s: Session): void {
  for (const e of s.events) {
    if (
      e.type === "agent_spawned" &&
      e.data.mode === "background" &&
      s.fold.children.get(e.data.child_thread_id) === "running" &&
      !s.fold.pending.has(e.data.call_id)
    )
      launch(s, e);
  }
}
