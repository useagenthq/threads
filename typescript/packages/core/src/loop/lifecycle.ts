import type { EventOf } from "../fold/state";
import type { SessionSource } from "../hooks/types";
import type { KnownEvent } from "../log";
import { draft } from "./drafts";
import {
  context,
  decision,
  defining,
  instruction,
  observe,
  recorded,
  run,
} from "./hooks";
import { endTurn } from "./request";
import type { Session } from "./session";
import { turnEvents } from "./turn";
import type { Halt, RunErrorCode } from "./types";

// Session, stop and notification hooks: on_stop gates the turn's end,
// session_start feeds a run's start, and the observation hooks see what a step appended.

/** on_stop continuations per turn before the turn ends stop_hook_limit. */
export const MAX_STOP_CONTINUATIONS = 3;

/**
 * A turn that would end end_turn asks on_stop first. `continue` forces one more request with
 * its reason as a trusted instruction; past the limit the turn ends stop_hook_limit. A failed
 * on_stop stops the run: a gate failure never continues it.
 */
export async function finish(s: Session): Promise<Halt | undefined> {
  const turn = turnEvents(s.events);
  const last = turn.findLast(
    (e) =>
      (e.type === "model_response" || e.type === "model_response_recovered") &&
      s.fold.requests.get(e.data.request_event_id)?.compaction !== true,
  );
  const key =
    last?.type === "model_response" || last?.type === "model_response_recovered"
      ? { request_event_id: last.data.request_event_id }
      : {};
  const continued = turn.filter(
    (e) =>
      e.type === "hook_decision" &&
      e.data.hook === "on_stop" &&
      e.data.decision === "continue",
  ).length;
  for (const ext of defining(s, "on_stop")) {
    if (recorded(turn, ext.name, "on_stop", key) !== undefined) continue;
    const out = await run(ext, "on_stop", [s.state()]);
    if (out.kind === "failed")
      return (
        s.append(decision(ext.name, "on_stop", "failed", key, out.reason)) ??
        endTurn(s, "end_turn")
      );
    if (out.value.decision === "stop") {
      const stopped = s.append(decision(ext.name, "on_stop", "stop", key));
      if (stopped !== undefined) return stopped;
      continue;
    }
    const { reason } = out.value;
    const recordedContinue = decision(
      ext.name,
      "on_stop",
      "continue",
      key,
      reason,
    );
    return continued >= MAX_STOP_CONTINUATIONS
      ? s.append(recordedContinue, draft.turnCompleted("stop_hook_limit"))
      : s.append(recordedContinue, instruction(ext.name, reason));
  }
  return endTurn(s, "end_turn");
}

/** session_start: a failure denies the session, so the run takes no input. */
export async function sessionStart(
  s: Session,
  source: SessionSource,
): Promise<Halt | undefined> {
  const got = await context(s, "session_start", [source]);
  return got === "failed"
    ? { code: "input_denied", message: "a session_start hook failed" }
    : got;
}

/** before_model_switch gates a settings_changed: a deny or failure keeps the current model. */
export async function switchGate(
  s: Session,
  settings: EventOf<"settings_changed">["data"]["settings"],
): Promise<Halt | boolean> {
  for (const ext of defining(s, "before_model_switch")) {
    const out = await run(ext, "before_model_switch", [settings]);
    const denied =
      out.kind === "failed"
        ? decision(ext.name, "before_model_switch", "failed", {}, out.reason)
        : out.value.decision === "deny"
          ? decision(
              ext.name,
              "before_model_switch",
              "deny",
              {},
              out.value.reason,
            )
          : undefined;
    const stopped = s.append(
      denied ?? decision(ext.name, "before_model_switch", "allow"),
    );
    if (stopped !== undefined) return stopped;
    if (denied !== undefined) return false;
  }
  return true;
}

/** Turn endings that are a run error: stop_failure fires for them, never on_stop. */
const FAILURES: Partial<
  Record<EventOf<"turn_completed">["data"]["reason"], RunErrorCode>
> = {
  context_exhausted: "context_exhausted",
  model_unavailable: "model_unavailable",
  max_output: "max_output",
  max_turns: "max_turns",
  output_invalid: "output_invalid",
  input_denied: "input_denied",
  error: "model_error",
};

const NOTIFY: ReadonlySet<KnownEvent["type"]> = new Set([
  "parked",
  "retry_scheduled",
  "budget_exceeded",
  "agent_finished",
]);

/** The observation hooks for what one loop step appended. They never change execution. */
export async function afterStep(
  s: Session,
  fromSeq: number,
): Promise<Halt | undefined> {
  if ((s.config.extensions ?? []).length === 0) return undefined;
  for (const e of s.events.filter((x) => x.seq > fromSeq)) {
    const stopped = await observed(s, e);
    if (stopped !== undefined) return stopped;
  }
  return undefined;
}

function observed(
  s: Session,
  e: KnownEvent,
): Promise<Halt | undefined> | undefined {
  if (NOTIFY.has(e.type)) return observe(s, "notification", [e]);
  if (e.type === "settings_changed")
    return observe(s, "after_model_switch", [e.data.settings]);
  if (e.type !== "turn_completed") return undefined;
  const code = FAILURES[e.data.reason];
  return code === undefined ? undefined : observe(s, "stop_failure", [code]);
}
