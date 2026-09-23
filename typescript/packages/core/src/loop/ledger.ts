import type { EventOf } from "../fold/state";
import type { KnownEvent } from "../log";
import { reservation, settlement, tokenBounds } from "../reduce/cost";
import type { Claim, LimitName } from "../store";
import type { Session } from "./session";
import { turnEvents } from "./turn";
import type { Covering } from "./types";

// Tree-wide budgets: before every model_request in any thread of a tree, its
// bound is reserved against every budget covering the thread (its own thread and run budgets and
// each ancestor's) in one transaction on the store's budget_ledger; the request is appended only
// once that commits. Its response settles the reservation to the attempt's disposition.

type Refusal = EventOf<"budget_exceeded">["data"];

const LIMITS: readonly LimitName[] = [
  "max_cost_nanos",
  "max_input_tokens",
  "max_output_tokens",
  "max_model_requests",
];

/** This thread's own budgets now: its thread budget and the open run's. */
export function ownCovering(s: Session): readonly Covering[] {
  const thread = s.fold.policy?.budget;
  const input = turnEvents(s.events)[0];
  const run = input?.type === "user_input" ? input.data.budget : undefined;
  return [
    ...(thread === undefined
      ? []
      : [
          {
            budgetId: `thread:${s.threadId}`,
            budget: thread,
            scope: "thread" as const,
          },
        ]),
    ...(run === undefined || input === undefined
      ? []
      : [
          {
            budgetId: `run:${s.threadId}:${input.event_id}`,
            budget: run,
            scope: "run" as const,
          },
        ]),
  ];
}

/** Every budget covering this thread: its own, then its ancestors'. */
export function covering(s: Session): readonly Covering[] {
  return [...ownCovering(s), ...(s.config.budgets?.inherited ?? [])];
}

/** What a child of this thread inherits: every budget covering this thread, as an ancestor's. */
export function inheritedBy(s: Session): readonly Covering[] {
  return covering(s).map((c) =>
    c.scope === "ancestor" ? c : { ...c, scope: "ancestor", owner: s.threadId },
  );
}

/**
 * Reserves the next attempt's bound (it is appended at the next seq) against every covering
 * budget, or returns the budget_exceeded the refusal records. Earlier attempts settle first.
 */
export function reserve(s: Session): Refusal | undefined {
  const budgets = s.config.budgets;
  if (budgets === undefined) return undefined;
  settleOpen(s);
  const amounts = nextAmounts(s);
  const all = covering(s);
  const claims = all.flatMap((c) =>
    LIMITS.flatMap((limit): Claim[] => {
      const max = c.budget[limit];
      const amount = amounts.get(limit);
      return max === undefined || amount === undefined
        ? []
        : [{ budgetId: c.budgetId, limit, max, amount }];
    }),
  );
  const refused = budgets.ledger.reserve(key(s, s.fold.seq + 1), claims);
  if (refused === undefined) return undefined;
  const by = all.find((c) => c.budgetId === refused.claim.budgetId);
  const common = {
    limit: refused.claim.limit,
    limit_value: refused.claim.max,
    observed: refused.observed,
    observed_is_upper_bound: refused.claim.limit !== "max_model_requests",
  };
  return by?.scope === "ancestor" && by.owner !== undefined
    ? { scope: "ancestor", owner_thread_id: by.owner, ...common }
    : { scope: by?.scope === "run" ? "run" : "thread", ...common };
}

const key = (s: Session, seq: number): string => `${s.branchId}:${seq}`;

/** The next attempt's bound per limit; a limit with no declared bound isn't reserved. */
function nextAmounts(s: Session): ReadonlyMap<LimitName, number> {
  const settings = s.fold.model;
  const epoch = s.events.findLast(
    (e) => e.type === "settings_changed" || e.type === "thread_started",
  );
  const params =
    epoch?.type === "settings_changed"
      ? epoch.data.settings.model_params
      : epoch?.type === "thread_started"
        ? epoch.data.model_params
        : {};
  const model = s.fold.policy?.models?.find(
    (m) => m.provider === settings?.provider && m.name === settings.name,
  );
  const bounds = tokenBounds(model, params, undefined);
  const cost =
    model?.price === undefined
      ? undefined
      : reservation(model, model.price, params, undefined);
  return defined([
    ["max_model_requests", 1],
    ["max_cost_nanos", cost],
    ["max_input_tokens", bounds.input],
    ["max_output_tokens", bounds.output],
  ]);
}

/**
 * Settles this branch's reserved attempts that now have a disposition, and releases a
 * reservation whose attempt never reached the log (the writer stopped between the two).
 */
export function settleOpen(s: Session): void {
  const budgets = s.config.budgets;
  if (budgets === undefined) return;
  const prefix = `${s.branchId}:`;
  for (const attempt of budgets.ledger.reserved(prefix)) {
    const seq = Number(attempt.slice(prefix.length));
    const request = s.events.find((e) => e.seq === seq);
    if (request?.type !== "model_request") {
      if (seq <= s.fold.seq) budgets.ledger.release(attempt);
      continue;
    }
    if (!settled(s.events, request.event_id)) continue;
    const got = settlement(s.events, s.fold.policy, request.event_id);
    budgets.ledger.settle(attempt, [
      ...defined([
        ["max_cost_nanos", got.cost],
        ["max_input_tokens", got.input],
        ["max_output_tokens", got.output],
      ]),
    ]);
  }
}

function settled(events: readonly KnownEvent[], requestId: string): boolean {
  return events.some(
    (e) =>
      (e.type === "model_response" ||
        e.type === "model_response_recovered" ||
        e.type === "model_attempt_abandoned") &&
      e.data.request_event_id === requestId,
  );
}

function defined(
  pairs: readonly (readonly [LimitName, number | undefined])[],
): Map<LimitName, number> {
  return new Map(
    pairs.flatMap(([k, v]) => (v === undefined ? [] : [[k, v] as const])),
  );
}
