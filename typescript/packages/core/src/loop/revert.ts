import type { EventOf } from "../fold/state";
import type { KnownEvent, Policy } from "../log";
import { draft } from "./drafts";
import { switchGate } from "./lifecycle";
import { retryPolicy } from "./policy";
import type { Session } from "./session";
import { BARRED, type Halt } from "./types";

// The turn-scoped fallback revert (ADR 0020): with fallback_scope turn, the next input goes
// back to the settings in force before the fallback, gated by before_model_switch.

type Settings = EventOf<"settings_changed">["data"]["settings"];
type Input = EventOf<"user_input">;

/**
 * The input that owes a revert and the settings to restore, or undefined. It is owed when the
 * current epoch was entered by a fallback, the scope is turn, the latest input came after that
 * fallback, and before_model_switch has not decided on that input yet (an allow reverted, a
 * deny keeps the fallback for the input's turn). Derived from the log alone, so a crash never
 * asks the hook twice for one input.
 */
function revertDue(
  events: readonly KnownEvent[],
  policy: Policy | undefined,
): { readonly input: Input; readonly settings: Settings } | undefined {
  if (retryPolicy(policy).fallback_scope !== "turn") return undefined;
  const changes = events.filter(
    (e): e is EventOf<"settings_changed"> => e.type === "settings_changed",
  );
  const last = changes.at(-1);
  const input = events.findLast((e): e is Input => e.type === "user_input");
  if (last?.data.reason !== "fallback" || input === undefined) return undefined;
  if (input.seq < last.seq || events.some((e) => decided(e, input)))
    return undefined;
  const before = changes.findLast((c) => c.data.reason !== "fallback");
  return { input, settings: before?.data.settings ?? pinned(events) };
}

/** A before_model_switch decision on `input`, recorded after it (an earlier one can't be). */
function decided(e: KnownEvent, input: Input): boolean {
  return (
    e.seq > input.seq &&
    e.type === "hook_decision" &&
    e.data.hook === "before_model_switch" &&
    e.data.input_event_id === input.event_id
  );
}

/** The settings thread_started pinned: the epoch a thread starts in. */
function pinned(events: readonly KnownEvent[]): Settings {
  const started = events.find((e) => e.type === "thread_started");
  if (started?.type !== "thread_started")
    throw new Error("a thread with a settings epoch has its thread_started");
  const { model, model_params, adapter } = started.data;
  return { model, model_params, adapter, reasoning_carryover: "keep" };
}

/**
 * Reverts a turn-scoped fallback before the turn's first request, when one is owed: "none" when
 * nothing was owed, else what appending the outcome returned. The hook's decisions and the revert
 * (or only the deny) are one batch: a crash leaves either nothing, so the hook is asked again, or
 * the whole outcome.
 */
export async function revert(s: Session): Promise<Halt | undefined | "none"> {
  const due = revertDue(s.events, s.fold.policy);
  if (due === undefined) return "none";
  const cause = due.input.event_id;
  const gate = await switchGate(s, due.settings, { input_event_id: cause });
  if (!gate.allowed) return s.append(...gate.decisions);
  const reverted = s.appendWork(
    ...gate.decisions,
    draft.settingsChanged({
      reason: "revert",
      settings: due.settings,
      cause_event_id: cause,
    }),
  );
  // A cancel landed during the hook: the revert waits for the next turn.
  return reverted === BARRED ? undefined : reverted;
}
