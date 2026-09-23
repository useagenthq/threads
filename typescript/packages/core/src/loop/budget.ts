import type { z } from "zod";
import type { EventOf } from "../fold/state";
import type { Budget, KnownEvent, Policy } from "../log";

// the limits a thread checks from its own log before a model_request: turns
// and wall time. Cost, tokens and requests are reserved tree-wide in the ledger (ledger.ts).

type Refusal = Omit<EventOf<"budget_exceeded">["data"], "owner_thread_id">;
type Limit = Refusal["limit"];

type Window = {
  readonly events: readonly KnownEvent[];
  readonly before: readonly KnownEvent[];
  readonly startedAt: number;
};

/** The first turn or wall-time limit of the thread or run budget the next attempt would exceed. */
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
    const refused = check(budget, window, now);
    if (refused !== undefined) return { scope, ...refused };
  }
  return undefined;
}

function check(
  budget: z.infer<typeof Budget>,
  w: Window,
  now: number,
): Omit<Refusal, "scope"> | undefined {
  const observed: readonly [Limit, number][] = [
    ["max_turns", count(w, "turn_completed") + 1],
    ["max_wall_ms", now - w.startedAt],
  ];
  for (const [limit, value] of observed) {
    const max = budget[limit];
    if (max !== undefined && value > max)
      return {
        limit,
        limit_value: max,
        observed: value,
        observed_is_upper_bound: false,
      };
  }
  return undefined;
}

function count(w: Window, type: KnownEvent["type"]): number {
  const all = w.events.filter((e) => e.type === type).length;
  return all - w.before.filter((e) => e.type === type).length;
}
