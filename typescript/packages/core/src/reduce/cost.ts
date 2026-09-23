import type { EventOf } from "../fold/state";
import type { KnownEvent, Policy } from "../log";

type PolicyModel = NonNullable<Policy["models"]>[number];
type Price = NonNullable<PolicyModel["price"]>;
type Usage = EventOf<"model_response">["data"]["usage"];
type Settings = {
  readonly model: EventOf<"thread_started">["data"]["model"];
  readonly params: Readonly<Record<string, unknown>>;
};

export type Cost = {
  readonly currency: string;
  readonly known_nanos: number;
  readonly upper_bound_nanos: number;
  readonly complete: boolean;
  readonly bounded: boolean;
};

type Attempt = {
  readonly id: string;
  readonly request: EventOf<"model_request">["data"];
  readonly settings: Settings;
  response?: EventOf<"model_response">["data"];
  abandon?: EventOf<"model_attempt_abandoned">["data"];
};

const USAGE_PRICES = [
  ["input_tokens", "input"],
  ["output_tokens", "output"],
  ["cache_read_tokens", "cache_read"],
  ["cache_write_tokens", "cache_write"],
] as const;

/** One attempt per model_request, priced by the settings epoch it was sent under. */
function attempts(events: readonly KnownEvent[]): readonly Attempt[] {
  const byId = new Map<string, Attempt>();
  let settings: Settings | undefined;
  for (const e of events) {
    settings = epochSettings(e) ?? settings;
    if (e.type === "model_request" && settings !== undefined)
      byId.set(e.event_id, { id: e.event_id, request: e.data, settings });
    else settle(byId, e);
  }
  return [...byId.values()];
}

/** The settings a thread_started or settings_changed starts. */
function epochSettings(e: KnownEvent): Settings | undefined {
  if (e.type === "thread_started")
    return { model: e.data.model, params: e.data.model_params };
  if (e.type !== "settings_changed") return undefined;
  const { model, model_params: params } = e.data.settings;
  return { model, params };
}

function settle(byId: Map<string, Attempt>, e: KnownEvent): void {
  if (e.type === "model_response" || e.type === "model_response_recovered") {
    const attempt = byId.get(e.data.request_event_id);
    if (attempt !== undefined) attempt.response = e.data;
  } else if (e.type === "model_attempt_abandoned") {
    const attempt = byId.get(e.data.request_event_id);
    if (attempt !== undefined) attempt.abandon = e.data;
  }
}

/** The per-attempt upper bound, or undefined when none is declared. */
export function reservation(
  model: PolicyModel,
  price: Price,
  params: Settings["params"],
  inputBound: number | undefined,
): number | undefined {
  const bounds = tokenBounds(model, params, inputBound);
  if (bounds.input === undefined || bounds.output === undefined)
    return undefined;
  const { input, cache_read = 0, cache_write = 0, output } = price;
  return (
    bounds.input * Math.max(input, cache_read, cache_write) +
    bounds.output * output
  );
}

/** Known nanos, and the upper bound (undefined when unbounded) for one response. */
function responseCost(
  usage: Usage,
  price: Price,
  bound: number | undefined,
): readonly [number, number | undefined] {
  let known = 0;
  let complete = true;
  for (const [field, key] of USAGE_PRICES) {
    const tokens = usage[field];
    if (tokens === null) complete = false;
    else if (tokens !== undefined) known += tokens * (price[key] ?? 0);
  }
  if (complete) return [known, known];
  return [known, bound === undefined ? undefined : known + bound];
}

function notBilled(attempt: Attempt): boolean {
  const abandon = attempt.abandon;
  return (
    abandon !== undefined &&
    (abandon.provider_outcome === "not_sent" ||
      abandon.billing === "not_billed")
  );
}

/** One attempt's [known, upper bound] nanos; undefined when its model has no price. */
function disposition(
  attempt: Attempt,
  models: readonly PolicyModel[],
): readonly [number, number | undefined] | undefined {
  if (attempt.response === undefined && notBilled(attempt)) return [0, 0];
  const { provider, name } = attempt.settings.model;
  const model = models.find((m) => m.provider === provider && m.name === name);
  const price = model?.price;
  if (model === undefined || price === undefined) return undefined;
  const bound = reservation(
    model,
    price,
    attempt.settings.params,
    attempt.request.input_bound_tokens,
  );
  return attempt.response === undefined
    ? [0, bound]
    : responseCost(attempt.response.usage, price, bound);
}

/**
 * One attempt as the budget ledger settles it: its cost at the upper bound,
 * and its tokens, each measured or else at the attempt's bound; nothing when proven unbilled.
 */
export function settlement(
  events: readonly KnownEvent[],
  policy: Policy | undefined,
  requestId: string,
): {
  readonly cost: number | undefined;
  readonly input: number | undefined;
  readonly output: number | undefined;
} {
  const attempt = attempts(events).find((a) => a.id === requestId);
  if (attempt === undefined) throw new Error(`no attempt ${requestId}`);
  const models = policy?.models ?? [];
  const d = disposition(attempt, models);
  const { provider, name } = attempt.settings.model;
  const model = models.find((m) => m.provider === provider && m.name === name);
  const bounds = tokenBounds(
    model,
    attempt.settings.params,
    attempt.request.input_bound_tokens,
  );
  if (attempt.response === undefined && notBilled(attempt))
    return { cost: 0, input: 0, output: 0 };
  const usage = attempt.response?.usage;
  return {
    cost: d?.[1],
    input: usage?.input_tokens ?? bounds.input,
    output: usage?.output_tokens ?? bounds.output,
  };
}

/** An attempt's token bounds: the declared input bound and the epoch's max_tokens. */
export function tokenBounds(
  model: PolicyModel | undefined,
  params: Settings["params"],
  inputBound: number | undefined,
): { readonly input?: number; readonly output?: number } {
  const input =
    inputBound ??
    (model?.input_billing_bound === "context_window"
      ? model.context_window
      : undefined);
  const { max_tokens: output } = params;
  return {
    ...(input === undefined ? {} : { input }),
    ...(typeof output === "number" ? { output } : {}),
  };
}

/** Projection `cost`: known cost and a conservative bound. */
export function cost(
  events: readonly KnownEvent[],
  policy: Policy | undefined,
): Cost | undefined {
  if (policy?.currency === undefined || policy.models === undefined)
    return undefined;
  let known = 0;
  let upper = 0;
  let complete = true;
  let bounded = true;
  for (const attempt of attempts(events)) {
    const d = disposition(attempt, policy.models);
    // An unpriced model can be neither costed nor bounded.
    if (d === undefined) {
      complete = false;
      bounded = false;
      continue;
    }
    const [k, u] = d;
    known += k;
    complete &&= u === k;
    bounded &&= u !== undefined;
    upper += u ?? k;
  }
  return {
    currency: policy.currency,
    known_nanos: known,
    upper_bound_nanos: upper,
    complete,
    bounded,
  };
}

/**
 * A tree total: `part` added into `total`. A part with no cost, or in another currency, can't be
 * added, so the total stops claiming to be complete or a bound rather than undercounting.
 */
export function mergeCost(total: Cost, part: Cost | undefined): Cost {
  if (part === undefined || part.currency !== total.currency)
    return { ...total, complete: false, bounded: false };
  return {
    currency: total.currency,
    known_nanos: total.known_nanos + part.known_nanos,
    upper_bound_nanos: total.upper_bound_nanos + part.upper_bound_nanos,
    complete: total.complete && part.complete,
    bounded: total.bounded && part.bounded,
  };
}
