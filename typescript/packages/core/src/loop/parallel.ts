import { type EventOf, loopParked, loopPending } from "../fold/state";
import { afterTool, body, recordRead, runAlone } from "./dispatch";
import { frameworkTool } from "./framework";
import { type Candidate, groups } from "./groups";
import type { Session } from "./session";
import { callSpec, cancelRequested } from "./turn";
import type { Halt, ToolRun } from "./types";

// Parallel tool calls (spec/schema/README.md): the pending calls of a step run as planned by
// groups(). A group's reads start together; their results are recorded in call order.

/** At most this many calls of a group are started and not yet recorded. */
const WINDOW = 8;

type Call = EventOf<"tool_call">;

/** The step over the pending calls, in call order: each group, then each call alone. */
export async function runCalls(s: Session): Promise<Halt | undefined> {
  const pending = loopPending(s.fold).flatMap((callId) => {
    const call = s.events.findLast(
      (e) => e.type === "tool_call" && e.data.call_id === callId,
    );
    return call?.type === "tool_call" ? [call] : [];
  });
  for (const group of groups(pending.map((c) => candidate(s, c)))) {
    // Nothing new starts after a cancel: the loop's next step closes the rest.
    if (cancelRequested(s.events, s.fold) !== undefined) return undefined;
    const calls = group.flatMap((i) => pending[i] ?? []);
    const [first] = calls;
    if (first === undefined) continue;
    const stopped =
      calls.length > 1 ? await runGroup(s, calls) : await runAlone(s, first);
    // A step that parked or ended the turn (a handoff) dispatches nothing more.
    if (
      stopped !== undefined ||
      loopParked(s.fold).length > 0 ||
      !s.fold.turnOpen
    )
      return stopped;
  }
  return undefined;
}

function candidate(s: Session, call: Call): Candidate {
  const { name, call_id: callId } = call.data;
  const spec = callSpec(s.fold, call);
  return {
    concurrent: s.config.tools.get(name)?.concurrent === true,
    effectClass: spec?.effect_class ?? "unguarded",
    framework: frameworkTool(s, name) !== undefined,
    endsTurn: spec?.ends_turn === true,
    decision: decision(s, callId),
  };
}

/** The recorded decision: an ask stays an ask after its approval, so it always runs alone. */
function decision(s: Session, callId: string): Candidate["decision"] {
  const recorded = s.events.findLast(
    (e) => e.type === "permission_decision" && e.data.call_id === callId,
  );
  return recorded?.type === "permission_decision"
    ? recorded.data.decision
    : "none";
}

/**
 * Starts the group's calls within the window and records each result, then after_tool, in
 * call order. A stale owner stops: nothing more is recorded, and the started bodies are
 * aborted and awaited so none outlives the run.
 */
async function runGroup(
  s: Session,
  calls: readonly Call[],
): Promise<Halt | undefined> {
  const group = new AbortController();
  const caller = s.config.signal;
  const signal =
    caller === undefined
      ? group.signal
      : AbortSignal.any([caller, group.signal]);
  const started: Promise<ToolRun>[] = [];
  let stopped: Halt | undefined;
  for (const [i, call] of calls.entries()) {
    stopped = await start(s, calls, started, i + WINDOW, signal);
    const running = started[i];
    // Not started: a failed fence, or a cancel before it.
    if (stopped !== undefined || running === undefined) break;
    const before = s.fold.seq;
    stopped =
      (await recordRead(s, call.data.call_id, await running)) ??
      (await afterTool(s, call, before));
    if (stopped !== undefined) break;
  }
  if (stopped !== undefined) {
    group.abort();
    await Promise.allSettled(started);
  }
  return stopped;
}

/** Starts queued calls up to `limit`, each fenced immediately before its body. */
async function start(
  s: Session,
  calls: readonly Call[],
  started: Promise<ToolRun>[],
  limit: number,
  signal: AbortSignal,
): Promise<Halt | undefined> {
  while (started.length < limit) {
    const next = calls[started.length];
    if (next === undefined || cancelRequested(s.events, s.fold) !== undefined)
      return undefined;
    const fenced = await s.fence();
    if (fenced !== undefined) return fenced;
    started.push(body(s, next, signal));
  }
  return undefined;
}
