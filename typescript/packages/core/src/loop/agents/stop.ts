import {
  endedOtherwise,
  type RunEnd,
  type RunStatus,
  runEnd,
} from "../../reduce/run-end";
import type { LoopEnd } from "../run";
import { runLoop } from "../run";
import type { Session } from "../session";
import type { Spawned } from "./spawn";

// An early end stops the run's children (spec/schema/README.md, "Run completion"): a run that
// ended failed, budget_exhausted or handed_off gives each background child it waits on (its
// own, and an earlier run's it relaunched) a durable tree cancel, waits the step each needs to
// stop, and records their ends with no wake. A cancel of the lead during that wait ends it.

const REASON = "parent run ended";
/** How often a child not barred yet is tried again: its thread may not exist at the first try. */
const RETRY_MS = 50;

/** How the latest request's run ended, if it has. */
function latestRun(s: Session): RunEnd | undefined {
  const request = s.events.findLast((e) => e.type === "user_input");
  return request === undefined ? undefined : runEnd(s.events, request.event_id);
}

/** The status of the latest request's run. */
export function runStatus(s: Session): RunStatus {
  return latestRun(s)?.status ?? "running";
}

/** The background children this run waits on and that have not reported: its own, and any
 * earlier run's it relaunched. */
function waitedOn(s: Session): readonly Spawned[] {
  const request = s.events.findLast((e) => e.type === "user_input");
  return s.events.filter(
    (e): e is Spawned =>
      e.type === "agent_spawned" &&
      e.data.mode === "background" &&
      s.fold.children.get(e.data.child_thread_id) === "running" &&
      (s.background.has(e.data.call_id) ||
        s.fold.team.spawns.get(e.data.call_id)?.root?.event ===
          request?.event_id),
  );
}

/** A thread or tree cancel of the lead after `seq`. */
function cancelledAfter(s: Session, seq: number): boolean {
  return s.events.some(
    (e) =>
      e.seq > seq && e.type === "cancel_requested" && e.data.scope !== "turn",
  );
}

async function bar(
  s: Session,
  left: readonly Spawned[],
  barred: Set<string>,
): Promise<void> {
  for (const { data } of left) {
    if (barred.has(data.child_thread_id)) continue;
    const sub = s.config.agents?.subagent(data.agent_name);
    if (await sub?.stop(data.child_thread_id, s.config.principal, REASON))
      barred.add(data.child_thread_id);
  }
}

/** Waits for a child's end or the lead's log to move, and while a child is not barred yet, at
 * most RETRY_MS so it is tried again. */
async function waitOn(
  running: readonly Promise<void>[],
  moved: Promise<void>,
  retry: boolean,
): Promise<void> {
  // ponytail: re-sends every 50 ms until each child has its barrier; a start notification
  // would do it without a timer.
  const timer = Promise.withResolvers<void>();
  const handle = retry ? setTimeout(timer.resolve, RETRY_MS) : undefined;
  await Promise.race(
    retry ? [...running, moved, timer.promise] : [...running, moved],
  );
  clearTimeout(handle);
}

/** The seq of the run's deciding event: a cancel of the lead after it ends the wait. */
function decidedSeq(s: Session): number {
  const at = latestRun(s)?.at;
  return at === undefined ? s.fold.seq : (s.events[at]?.seq ?? s.fold.seq);
}

export async function stopChildren(
  s: Session,
  first: LoopEnd,
): Promise<LoopEnd> {
  let end = first;
  const decided = decidedSeq(s);
  const barred = new Set<string>();
  for (let left = waitedOn(s); left.length > 0; left = waitedOn(s)) {
    if (end.kind === "halted" || !endedOtherwise(runStatus(s))) return end;
    // Taken before the checks: a cancel that lands after them still wakes the wait.
    const moved = s.moved();
    if (cancelledAfter(s, decided)) return end;
    await bar(s, left, barred);
    const running = left.flatMap((e) => s.background.get(e.data.call_id) ?? []);
    if (running.length === 0 && s.finished.size === 0) return end;
    const retry = left.some((e) => !barred.has(e.data.child_thread_id));
    if (s.finished.size === 0) await waitOn(running, moved, retry);
    end = await runLoop(s);
  }
  return end;
}
