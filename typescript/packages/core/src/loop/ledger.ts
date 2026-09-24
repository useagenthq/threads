import type { EventOf, Fold } from "../fold/state";
import type { KnownEvent, ThreadId } from "../log";
import { reservation, settlement, tokenBounds } from "../reduce/cost";
import type { Claim, LimitName } from "../store";
import type { Session } from "./session";
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

/** What a thread's own budgets are read from: its log, as of some point. */
type View = {
  readonly threadId: ThreadId;
  readonly fold: Pick<Fold, "policy">;
  readonly events: readonly KnownEvent[];
};

/** A thread's own budgets as of its `events`: its thread budget and the open run's. */
export function ownCovering(s: View): readonly Covering[] {
  const thread = s.fold.policy?.budget;
  // A wake turn is under the budget of the latest run's input, as refusal() counts it.
  const input = s.events.findLast((e) => e.type === "user_input");
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

/**
 * What a thread started by this one inherits (a child, or a handoff target: Handoff scope):
 * every budget covering this thread, as an ancestor's.
 */
export function inheritedFrom(
  s: View,
  inherited: readonly Covering[],
): readonly Covering[] {
  return [...ownCovering(s), ...inherited].map((c) =>
    c.scope === "ancestor" ? c : { ...c, scope: "ancestor", owner: s.threadId },
  );
}

/** What a child of this thread inherits. */
export function inheritedBy(s: Session): readonly Covering[] {
  return inheritedFrom(s, s.config.budgets?.inherited ?? []);
}

/**
 * Reserves the next attempt's bound (it is appended at the next seq) against every covering
 * budget, or returns the budget_exceeded the refusal records. The ledger is first brought up to
 * date from the log, and earlier attempts settle first.
 */
export function reserve(s: Session): Refusal | undefined {
  const budgets = s.config.budgets;
  if (budgets === undefined) return undefined;
  rebuild(s);
  settleOpen(s);
  const all = covering(s);
  const claims = claimsOf(all, nextAmounts(s, s.events));
  // A limit this attempt has no bound for is refused, never skipped (Budget enforcement).
  const unbounded = claims.find((c) => c.amount === undefined);
  const refused =
    unbounded === undefined
      ? budgets.ledger.reserve(key(s, s.fold.seq + 1), claims.filter(bounded))
      : {
          claim: { ...unbounded, amount: 0 },
          observed: budgets.ledger.spent(unbounded.budgetId, unbounded.limit),
        };
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

/** A claim whose amount is undefined when the attempt has no bound for its limit. */
type Open = Omit<Claim, "amount"> & { readonly amount: number | undefined };

const bounded = (c: Open): c is Claim => c.amount !== undefined;

function claimsOf(
  all: readonly Covering[],
  amounts: ReadonlyMap<LimitName, number>,
): readonly Open[] {
  return all.flatMap((c) =>
    LIMITS.flatMap((limit): Open[] => {
      const max = c.budget[limit];
      return max === undefined
        ? []
        : [{ budgetId: c.budgetId, limit, max, amount: amounts.get(limit) }];
    }),
  );
}

/**
 * Re-enters, at its bound, every model_request of this branch the ledger lacks, so a lost,
 * wiped or imported ledger never resets a budget (invariant 1); settleOpen then settles them.
 * ponytail: this branch's attempts only; a finished descendant's are re-entered when it runs again.
 */
function rebuild(s: Session): void {
  const budgets = s.config.budgets;
  if (budgets === undefined) return;
  const known = budgets.ledger.keys(`${s.branchId}:`);
  s.events.forEach((e, i) => {
    if (e.type !== "model_request" || e.branch_id !== s.branchId) return;
    const attempt = key(s, e.seq);
    if (known.has(attempt)) return;
    const before = s.events.slice(0, i);
    const view = { threadId: s.threadId, fold: s.fold, events: before };
    const all = [...ownCovering(view), ...budgets.inherited];
    budgets.ledger.restore(
      attempt,
      claimsOf(all, nextAmounts(s, before)).filter(bounded),
    );
  });
}

const key = (s: Session, seq: number): string => `${s.branchId}:${seq}`;

/** The bound per limit of an attempt after `events`; a limit with no declared bound is absent. */
function nextAmounts(
  s: Session,
  events: readonly KnownEvent[],
): ReadonlyMap<LimitName, number> {
  const epoch = events.findLast(
    (e) => e.type === "settings_changed" || e.type === "thread_started",
  );
  const [ref, params] =
    epoch?.type === "settings_changed"
      ? [epoch.data.settings.model, epoch.data.settings.model_params]
      : epoch?.type === "thread_started"
        ? [epoch.data.model, epoch.data.model_params]
        : [undefined, {}];
  const model = s.fold.policy?.models?.find(
    (m) => m.provider === ref?.provider && m.name === ref.name,
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
