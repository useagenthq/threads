import { endedOtherwise, type RunStatus, runEnd } from "../../reduce/run-end";
import type { LoopEnd } from "../run";
import { runLoop } from "../run";
import type { Session } from "../session";
import type { Spawned } from "./spawn";

// An early end stops the run's children (spec/schema/README.md, "Run completion"): a run that
// ended failed, budget_exhausted or handed_off gives each of its background children that has
// not reported a durable tree cancel, waits the step each needs to stop, and records their ends
// with no wake, so no running child or pending_wakes row outlives run().

const REASON = "parent run ended";
/** How often an unstarted child is barred again: its thread may not exist at the first try. */
const RETRY_MS = 50;

/** The status of the latest request's run. */
export function runStatus(s: Session): RunStatus {
  const request = s.events.findLast((e) => e.type === "user_input");
  return request === undefined
    ? "running"
    : runEnd(s.events, request.event_id).status;
}

/** This run's background children still running or not yet recorded. */
function unreported(s: Session): readonly Spawned[] {
  const request = s.events.findLast((e) => e.type === "user_input");
  return s.events.filter(
    (e): e is Spawned =>
      e.type === "agent_spawned" &&
      e.data.mode === "background" &&
      s.fold.children.get(e.data.child_thread_id) === "running" &&
      s.fold.team.spawns.get(e.data.call_id)?.root?.event === request?.event_id,
  );
}

export async function stopChildren(
  s: Session,
  first: LoopEnd,
): Promise<LoopEnd> {
  let end = first;
  for (let left = unreported(s); left.length > 0; left = unreported(s)) {
    if (end.kind === "halted" || !endedOtherwise(runStatus(s))) return end;
    for (const spawned of left)
      await s.config.agents
        ?.subagent(spawned.data.agent_name)
        ?.stop(spawned.data.child_thread_id, s.config.principal, REASON);
    const running = left.flatMap((e) => s.background.get(e.data.call_id) ?? []);
    // A child launched but not yet started gets its barrier on a later pass.
    // ponytail: polls every 50 ms; a start notification would do it without a timer.
    const tick = Promise.withResolvers<void>();
    const timer = setTimeout(tick.resolve, RETRY_MS);
    if (s.finished.size === 0 && running.length > 0)
      await Promise.race([...running, s.moved(), tick.promise]);
    clearTimeout(timer);
    if (running.length === 0 && s.finished.size === 0) return end;
    end = await runLoop(s);
  }
  return end;
}
