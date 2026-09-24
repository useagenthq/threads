import { sameRun, type TurnRun } from "../../fold/team";
import type { EventDraft } from "../../store";
import type { Session } from "../session";
import type { ChildEnd, Halt } from "../types";
import { stopGate } from "./gates";
import { parkOn } from "./park";
import { endDrafts, finished, launch, type Spawned, spawnedFor } from "./spawn";
import { wakeDraft } from "./wake";

// Background children (F7.2): each runs concurrently in this process and hands its end to the
// parent's loop, which records it at a step boundary, so the parent's single writer never
// interleaves a late result into a step. A result recorded while no turn is open wakes the
// parent in the same append (spec/schema/README.md, "Background wakes").

type Ended = { readonly spawned: Spawned; readonly end: ChildEnd };
type Done = { readonly spawned: Spawned; readonly end: ChildDone };
type ChildDone = Exclude<ChildEnd, { status: "parked" }>;

const same = (a: TurnRun | undefined, b: TurnRun | undefined): boolean =>
  a === undefined || b === undefined ? a === b : sameRun(a, b);

/**
 * Records every child that ended since the last step and may be recorded now: a park, or
 * subagent_stop and then the late result. A child that couldn't run halts this run; it is
 * launched again on the next one.
 */
export async function settleBackground(s: Session): Promise<Halt | undefined> {
  const ready: Done[] = [];
  for (const [callId, end] of s.finished)
    if ("code" in end) {
      s.finished.delete(callId);
      return end;
    }
  for (const { spawned, end } of recordable(s)) {
    s.finished.delete(spawned.data.call_id);
    if (end.status === "parked") {
      const stopped = parkOn(s, spawned, end.reason);
      if (stopped !== undefined) return stopped;
      continue;
    }
    const next = await stopGate(s, spawned, finished(s, spawned, end));
    if (next === "continue") launch(s, spawned);
    else if (next !== "stop") return next;
    else ready.push({ spawned, end });
  }
  return ready.length === 0 ? undefined : record(s, ready);
}

/**
 * The ended children this boundary records, in spawn order. The writer rule: results of one
 * run at a time, so a result of another run than the open turn's (or, with no turn open, than
 * the first run to report) waits in `s.finished` until no turn is open. A park never waits.
 */
function recordable(s: Session): readonly Ended[] {
  const ended = [...s.finished].flatMap(([callId, end]): Ended[] => {
    if ("code" in end) return [];
    const spawned = spawnedFor(s, callId);
    if (spawned === undefined)
      throw new Error("a background child was spawned");
    return [{ spawned, end }];
  });
  const inOrder = ended.toSorted((a, b) => a.spawned.seq - b.spawned.seq);
  const { turnOpen, cancelled, team } = s.fold;
  const runOf = (c: Ended): TurnRun | undefined =>
    team.spawns.get(c.spawned.data.call_id);
  const first = inOrder.find((c) => c.end.status !== "parked");
  const target = turnOpen ? team.turn : first && runOf(first);
  return inOrder.filter(
    (c) =>
      c.end.status === "parked" ||
      (!turnOpen && cancelled) ||
      same(runOf(c), target),
  );
}

/** One append: each child's agent_finished and late result, and the wake when no turn is open. */
function record(s: Session, ready: readonly Done[]): Halt | undefined {
  const drafts: EventDraft[] = ready.flatMap(({ spawned, end }) =>
    endDrafts(s, spawned, end, true),
  );
  const causes = drafts.flatMap((d) =>
    d.type === "tool_result_late" && d.event_id !== undefined
      ? [d.event_id]
      : [],
  );
  const [first] = ready;
  const calls = ready.map((r) => r.spawned.data.call_id);
  const wake =
    first === undefined
      ? undefined
      : wakeDraft(s, first.spawned, calls, causes);
  return s.append(...drafts, ...(wake === undefined ? [] : [wake]));
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
