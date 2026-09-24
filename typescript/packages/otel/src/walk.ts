import {
  type ChainEvent,
  type KnownEvent,
  turnOpeners,
} from "@threads/core/internal/feed";
import { Calls } from "./calls";
import { Models } from "./models";
import type { Link, Span } from "./span";
import { type ParentOf, Turns } from "./turns";

// One pass over a resolved chain in seq order: every span that closes on it, and the span open at
// each agent_spawned and handoff (a child thread's parent). spec/otel/README.md, "Spans".

export type Walked = {
  /** Every closed span, the parent prefix's included, in close order. */
  readonly spans: readonly Span[];
  /** The span open at each agent_spawned (its call's) and handoff (its turn), by event id. */
  readonly anchors: ReadonlyMap<string, Link>;
};

export type WalkInput = {
  readonly tenant: string;
  /** The branch being exported; it names every span's threads.branch_id. */
  readonly branchId: string;
  readonly chain: readonly ChainEvent[];
  readonly content: boolean;
  readonly parentOf: ParentOf;
};

export function walk(input: WalkInput): Walked {
  return new Walk(input).run();
}

class Walk {
  readonly #spans: Span[] = [];
  readonly #anchors = new Map<string, Link>();
  readonly #calls: Calls;
  readonly #models: Models;
  readonly #turns: Turns;
  #agent = "agent";

  constructor(readonly input: WalkInput) {
    this.#calls = new Calls(input.content);
    this.#models = new Models(input.content);
    this.#turns = new Turns(input.parentOf, () => this.#agent);
  }

  run(): Walked {
    const openers = turnOpeners(this.input.chain);
    for (const line of this.input.chain) {
      if (line.kind !== "event") continue;
      const e = line.event;
      this.#config(e);
      this.#turns.see(e);
      this.#calls.see(e);
      this.#open(e, openers.has(e.event_id));
      this.#apply(e);
    }
    return { spans: this.#spans, anchors: this.#anchors };
  }

  #config(e: KnownEvent): void {
    if (e.type === "thread_started") {
      this.#agent = e.data.agent_name;
      this.#models.model = e.data.model;
      this.#tools(e.data.tools);
    } else if (e.type === "tools_changed") this.#tools(e.data.tools);
    else if (e.type === "settings_changed")
      this.#models.model = e.data.settings.model;
  }

  #tools(
    tools: readonly { readonly name: string; readonly effect_class: string }[],
  ): void {
    this.#calls.effectClasses = new Map(
      tools.map((t) => [t.name, t.effect_class]),
    );
  }

  /** A turn opener opens a turn; the first other event after a park opens the resumed one. */
  #open(e: KnownEvent, opener: boolean): void {
    if (opener) {
      this.#turns.begin(e);
      return;
    }
    const parked = this.#turns.parked;
    if (
      parked !== undefined &&
      this.#turns.open === undefined &&
      e.type !== "parked"
    )
      this.#turns.resume(e, parked);
  }

  #apply(e: KnownEvent): void {
    const turn = this.#turns.open;
    if (turn === undefined) return;
    const { branchId, tenant } = this.input;
    if (e.type === "model_request") this.#models.request(e, turn);
    else if (
      e.type === "model_response" ||
      e.type === "model_response_recovered" ||
      e.type === "model_attempt_abandoned"
    )
      this.#close(this.#models.close(e, branchId, tenant));
    else if (e.type === "retry_scheduled") this.#models.retry(e);
    else if (e.type === "tool_call") {
      this.#turns.callContexts.set(e.data.call_id, turn.context);
      this.#calls.call(e, turn);
    } else if (e.type === "tool_result")
      this.#close(this.#calls.result(e, branchId, tenant));
    else if (e.type === "resumed") this.#calls.resume(e, turn);
    else if (e.type === "parked" || e.type === "turn_completed") this.#end(e);
    else this.#note(e);
  }

  /** Span events, and the anchors a child thread's first turn is parented to. */
  #note(e: KnownEvent): void {
    const turn = this.#turns.open;
    if (turn === undefined) return;
    if (e.type === "agent_spawned") {
      const span = this.#calls.open.get(e.data.call_id);
      if (span !== undefined) this.#anchors.set(e.event_id, span.id);
    } else if (e.type === "handoff")
      this.#anchors.set(e.event_id, turn.span.id);
    else if (
      e.type === "permission_decision" ||
      e.type === "approval_requested" ||
      e.type === "approval_granted" ||
      e.type === "approval_denied" ||
      e.type === "effect_begin" ||
      e.type === "effect_commit" ||
      e.type === "effect_unknown" ||
      e.type === "effect_resolved"
    ) {
      if (!this.#calls.note(e.data.call_id, e)) this.#turns.note(e);
    } else if (
      e.type === "tool_result_late" ||
      e.type === "compacted" ||
      e.type === "compaction_failed" ||
      e.type === "budget_exceeded"
    )
      this.#turns.note(e);
  }

  /** A park or turn_completed closes the turn's model and call spans, then the turn. */
  #end(e: KnownEvent): void {
    const turn = this.#turns.open;
    if (turn === undefined) return;
    const { branchId, tenant } = this.input;
    this.#spans.push(...this.#models.endAll(e, turn, branchId, tenant));
    this.#spans.push(...this.#calls.endAll(e, branchId, tenant));
    this.#close(this.#turns.end(e, branchId, tenant));
  }

  #close(span: Span | undefined): void {
    if (span !== undefined) this.#spans.push(span);
  }
}
