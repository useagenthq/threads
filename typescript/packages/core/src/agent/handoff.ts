import type { EventOf } from "../fold/state";
import type { LoopConfig } from "../loop";
import { inheritedFrom } from "../loop/ledger";
import { knownEvents } from "../reduce";
import type { EventDraft, Writer } from "../store";
import type { Thread } from "../thread/handle";
import { execute } from "./execute";
import { type ChildEnv, type TargetFactory, targetFactory } from "./registry";
import type { RunResult } from "./result";
import { ceilingsOf, inheritedOf, type Plan, type Resolved } from "./run";

// A handoff's target: a new thread of the listed agent with its own pinned
// line 0 and policy, under the originating principal. The forwarded history arrives as
// untrusted reference, then the pending request repeats as its first input.

export function target<Deps, Output>(
  def: Resolved<Deps, Output>,
): TargetFactory {
  return (env) => async (link) => {
    const { principal } = env;
    const injected: EventDraft = {
      type: "injected",
      type_version: 1,
      critical: true,
      actor: { kind: "host" },
      data: {
        source: "handoff",
        trust: "untrusted_reference",
        origin: { id: link.parent.thread_id },
        ...(link.forwarded === undefined ? {} : { ref: link.forwarded }),
      },
    };
    const request: EventDraft = {
      type: "user_input",
      type_version: 1,
      critical: true,
      actor: { kind: "user", principal },
      data: { source: "handoff", text: link.request },
    };
    const result = await execute(
      def,
      {
        store: env.store,
        principal,
        target: {
          threadId: link.threadId,
          parent: link.parent,
          prefix: [injected],
        },
        ...(env.signal === undefined ? {} : { signal: env.signal }),
        ceilings: env.ceilings ?? [],
        ...(env.chain === undefined ? {} : { chain: env.chain }),
        ...(env.covering === undefined ? {} : { covering: env.covering }),
      },
      [request],
    );
    return result.thread;
  };
}

/** A handed-off thread's run result: its target, started (or resumed) from the recorded handoff. */
export async function handedOff<Deps, Output>(
  def: Resolved<Deps, Output>,
  plan: Plan<Deps>,
  writer: Writer,
  thread: Thread,
  authorize: LoopConfig["authorize"],
): Promise<RunResult<Output>> {
  const events = knownEvents(writer.chain);
  const view = { threadId: thread.id, fold: writer.chain.fold, events };
  // Handoff scope: the target runs inside this run's ceilings and budgets, and a subagent's
  // target inside the subagent's own decision chain.
  const chain: ChildEnv["chain"] =
    plan.child === undefined
      ? plan.chain
      : (call) => authorize(call, writer.chain.fold);
  const handoff = events.findLast(
    (e): e is EventOf<"handoff"> => e.type === "handoff",
  );
  const found = def.targets.find((a) => a.name === handoff?.data.to_agent);
  const factory = found === undefined ? undefined : targetFactory(found);
  if (handoff === undefined || factory === undefined)
    throw new Error("a pinned handoff target is configured");
  const input = events.findLast((e) => e.type === "user_input");
  const to_thread = await factory({
    store: plan.store,
    principal: plan.principal,
    ceilings: ceilingsOf(plan),
    covering: inheritedFrom(view, inheritedOf(plan)),
    ...(chain === undefined ? {} : { chain }),
    ...(plan.signal === undefined ? {} : { signal: plan.signal }),
  })({
    threadId: handoff.data.to_thread_id,
    parent: {
      thread_id: thread.id,
      branch_id: thread.branch,
      event_id: handoff.event_id,
      relation: "handoff",
    },
    ...(handoff.data.forwarded_ref === undefined
      ? {}
      : { forwarded: handoff.data.forwarded_ref }),
    request: input?.type === "user_input" ? (input.data.text ?? "") : "",
  });
  return { status: "handed_off", thread, to_thread };
}
