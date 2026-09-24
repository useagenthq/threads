import type { OfflineReason } from "../evals/files";
import { USER_EVENTS } from "../evals/kinds";
import { CHILD_TOOLS, TEAM_TOOLS } from "../evals/offline";
import type { EventOf } from "../fold/state";
import type { EventId, KnownEvent } from "../log";
import { err, ok, type Result } from "../result";
import type { Chain } from "../verify";
import { type LogError, logError } from "../verify/error";

// The turn a saved case replays (spec lane 22, A.1): a completed turn, found by its user_input,
// the last completed one by default. Its run's leading session_start decisions and its trailing
// observation decisions belong to it, so the rerun reproduces them too. The case log is the
// branch through the event before the run began.

type Input = EventOf<"user_input">;

export type Turn = {
  /** The seq the case log ends at: the last event before the turn's run appended anything. */
  readonly restoreSeq: number;
  readonly input: Input;
  /** What the turn's run appended, in order: what a rerun must append again. */
  readonly events: readonly KnownEvent[];
  /** Event types in the turn this build doesn't know: user code the rerun can't script. */
  readonly unknown: readonly string[];
};

const invalid = (message: string): Result<never, LogError> =>
  err(logError("invalid_request", message));

/** The user_input `at` names: the input itself, or the first input after a snapshot. */
function inputAt(
  events: readonly KnownEvent[],
  at: EventId | undefined,
): Result<number, LogError> {
  if (at === undefined) {
    const done = events.findLastIndex((e) => e.type === "turn_completed");
    const input = events.findLastIndex(
      (e, i) => i < done && e.type === "user_input",
    );
    return input === -1 ? invalid("no completed turn to save") : ok(input);
  }
  const named = events.findIndex((e) => e.event_id === at);
  const event = events[named];
  if (event?.type === "user_input") return ok(named);
  if (event?.type === "snapshot") {
    const next = events.findIndex(
      (e, i) => i > named && e.type === "user_input",
    );
    if (next !== -1) return ok(next);
  }
  return invalid(
    `no turn at ${at}: name its user_input or a snapshot before it`,
  );
}

/** session_start decisions (and their injections) the turn's run appended before its input. */
function leading(events: readonly KnownEvent[], input: number): number {
  let start = input;
  while (start > 0) {
    const e = events[start - 1];
    const sessionStart =
      e?.type === "hook_decision" && e.data.hook === "session_start";
    const injection = e?.type === "injected" && e.data.source === "hook";
    if (!sessionStart && !injection) break;
    start -= 1;
  }
  const opened = events
    .slice(start, input)
    .some((e) => e.type === "hook_decision");
  return opened ? start : input;
}

/** The observation decisions the run appended once the turn ended (stop_failure, session_end). */
function trailing(events: readonly KnownEvent[], done: number): number {
  let end = done;
  while (events[end + 1]?.type === "hook_decision") end += 1;
  return end;
}

export function findTurn(
  chain: Chain,
  at: EventId | undefined,
): Result<Turn, LogError> {
  const events = chain.events.flatMap((l) =>
    l.kind === "event" ? [l.event] : [],
  );
  const found = inputAt(events, at);
  if (!found.ok) return found;
  const input = events[found.value];
  if (input?.type !== "user_input")
    throw new Error("inputAt finds a user_input");
  const done = events.findIndex(
    (e, i) =>
      i > found.value &&
      (e.type === "turn_completed" || e.type === "user_input"),
  );
  if (events[done]?.type !== "turn_completed")
    return invalid(`the turn at ${input.event_id} is not completed`);
  const start = leading(events, found.value);
  const end = trailing(events, done);
  const first = events[start];
  const last = events[end];
  if (first === undefined || last === undefined)
    throw new Error("a turn has events");
  const unknown = chain.events.flatMap((l) =>
    l.kind === "unknown_event" &&
    l.event.seq >= first.seq &&
    l.event.seq <= last.seq
      ? [l.event.type]
      : [],
  );
  return ok({
    restoreSeq: first.seq - 1,
    input,
    events: events.slice(start, end + 1),
    unknown: [...new Set(unknown)],
  });
}

/** A child thread's model calls aren't in the case; neither are a team's member threads. */
const CHILD_EVENTS: ReadonlySet<string> = new Set([
  "agent_spawned",
  "handoff",
  "team_opened",
  "member_started",
]);
const TEAM_EVENTS: ReadonlySet<string> = new Set(["message_received", "woken"]);

const called = (turn: Turn, names: ReadonlySet<string>): boolean =>
  turn.events.some((e) => e.type === "tool_call" && names.has(e.data.name));

/** An effect the turn began that nothing settled: no result to stub. */
function unsettled(turn: Turn, later: readonly KnownEvent[]): boolean {
  const settled = new Set(
    later.flatMap((e) =>
      e.type === "effect_commit" || e.type === "effect_resolved"
        ? [e.data.call_id]
        : [],
    ),
  );
  return turn.events.some(
    (e) => e.type === "effect_begin" && !settled.has(e.data.call_id),
  );
}

/** Injected sources no list classifies: user code this build can't script. */
function unscripted(turn: Turn): readonly string[] {
  const known = new Set([
    ...USER_EVENTS.scriptable.sources,
    ...USER_EVENTS.framework.sources,
    ...USER_EVENTS.child_thread.sources,
  ]);
  const sources = turn.events.flatMap((e) =>
    e.type === "injected" && !known.has(e.data.source)
      ? [`injected:${e.data.source}`]
      : [],
  );
  return [...turn.unknown, ...new Set(sources)];
}

/** Why the turn can't rerun offline from the case alone (artifact_missing is found on copy). */
export function offlineReason(
  turn: Turn,
  later: readonly KnownEvent[],
):
  | { readonly reason: OfflineReason; readonly types?: readonly string[] }
  | undefined {
  if (turn.input.data.text === undefined) return { reason: "content_input" };
  if (
    called(turn, CHILD_TOOLS) ||
    turn.events.some((e) => CHILD_EVENTS.has(e.type))
  )
    return { reason: "child_threads" };
  if (
    called(turn, TEAM_TOOLS) ||
    turn.events.some((e) => TEAM_EVENTS.has(e.type))
  )
    return { reason: "team_calls" };
  if (unsettled(turn, later)) return { reason: "unsettled_effect" };
  const types = unscripted(turn);
  return types.length === 0 ? undefined : { reason: "extension_events", types };
}
