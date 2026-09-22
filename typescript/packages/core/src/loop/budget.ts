import type { z } from "zod";
import type { EventOf } from "../fold/state";
import type { Budget, KnownEvent, Policy } from "../log";
import { cost, reservation } from "../reduce/cost";

// reserve an attempt's bound before its model_request. The check and the
// append run under the single fenced writer, so nothing else can spend in between. Budgets of
// ancestor threads need the host's budget_ledger and come with subagents.

type Refusal = Omit<EventOf<"budget_exceeded">["data"], "owner_thread_id">;
type Limit = Refusal["limit"];

type Window = {
  readonly events: readonly KnownEvent[];
  readonly before: readonly KnownEvent[];
  readonly startedAt: number;
};

/** The first limit of the thread or run budget the next attempt would exceed, if any. */
export function refusal(
  events: readonly KnownEvent[],
  policy: Policy | undefined,
  now: number,
): Refusal | undefined {
  const run = events.findLastIndex((e) => e.type === "user_input");
  const input = events[run];
  const runBudget =
    input?.type === "user_input" ? input.data.budget : undefined;
  const scopes = [
    [
      "thread",
      policy?.budget,
      { events, before: [], startedAt: events[0]?.time ?? now },
    ],
    [
      "run",
      runBudget,
      { events, before: events.slice(0, run), startedAt: input?.time ?? now },
    ],
  ] as const;
  for (const [scope, budget, window] of scopes) {
    if (budget === undefined) continue;
    const refused = check(budget, window, policy, now);
    if (refused !== undefined) return { scope, ...refused };
  }
  return undefined;
}

function check(
  budget: z.infer<typeof Budget>,
  w: Window,
  policy: Policy | undefined,
  now: number,
): Omit<Refusal, "scope"> | undefined {
  const observed: readonly [Limit, number | undefined, boolean][] = [
    ["max_model_requests", count(w, "model_request") + 1, false],
    ["max_turns", count(w, "turn_completed") + 1, false],
    ["max_wall_ms", now - w.startedAt, false],
    ["max_cost_nanos", costWith(w, policy), true],
  ];
  for (const [limit, value, upper] of observed) {
    const max = budget[limit];
    if (max !== undefined && value !== undefined && value > max)
      return {
        limit,
        limit_value: max,
        observed: value,
        observed_is_upper_bound: upper,
      };
  }
  return undefined;
}

function count(w: Window, type: KnownEvent["type"]): number {
  const all = w.events.filter((e) => e.type === type).length;
  return all - w.before.filter((e) => e.type === type).length;
}

/** Spent so far at its upper bound, plus the next attempt's bound. */
function costWith(w: Window, policy: Policy | undefined): number | undefined {
  const spent = cost(w.events, policy);
  const earlier = cost(w.before, policy)?.upper_bound_nanos ?? 0;
  const bound = nextBound(w.events, policy);
  if (spent === undefined || bound === undefined) return undefined;
  return spent.upper_bound_nanos - earlier + bound;
}

function nextBound(
  events: readonly KnownEvent[],
  policy: Policy | undefined,
): number | undefined {
  const epoch = events.findLast(
    (e) => e.type === "settings_changed" || e.type === "thread_started",
  );
  const settings =
    epoch?.type === "settings_changed"
      ? epoch.data.settings
      : epoch?.type === "thread_started"
        ? epoch.data
        : undefined;
  if (settings === undefined) return undefined;
  const { provider, name } = settings.model;
  const model = policy?.models?.find(
    (m) => m.provider === provider && m.name === name,
  );
  if (model?.price === undefined) return undefined;
  return reservation(model, model.price, settings.model_params, undefined);
}
