import type { ThreadRef } from "../agent/result";
import { openStore, type Store } from "../agent/sqlite";
import { eventsOf } from "./live-env";

// What a live thread and its subagents spent, read off the log (spec lane 32, B.5). Counting
// from a baseline seq is what makes a continued prefix free: the imported real turns are below
// it, so only what this conversation appended is charged to its budget.

export type Totals = {
  readonly requests: number;
  readonly inputTokens: number;
  readonly outputTokens: number;
  readonly turns: number;
};

export const NO_TOTALS: Totals = {
  requests: 0,
  inputTokens: 0,
  outputTokens: 0,
  turns: 0,
};

function addTotals(a: Totals, b: Totals): Totals {
  return {
    requests: a.requests + b.requests,
    inputTokens: a.inputTokens + b.inputTokens,
    outputTokens: a.outputTokens + b.outputTokens,
    turns: a.turns + b.turns,
  };
}

/** model_requests, known token counts and completed turns of a thread and its subagents. */
export async function treeTotals(
  store: Store,
  thread: ThreadRef,
  sinceSeq = 0,
): Promise<Totals> {
  const { log } = await openStore(store);
  const events = (await eventsOf(store, thread)).filter(
    (e) => e.seq > sinceSeq,
  );
  let totals: Totals = {
    requests: events.filter((e) => e.type === "model_request").length,
    inputTokens: 0,
    outputTokens: 0,
    turns: events.filter((e) => e.type === "turn_completed").length,
  };
  for (const e of events) {
    if (e.type === "model_response")
      totals = addTotals(totals, {
        ...NO_TOTALS,
        inputTokens: e.data.usage.input_tokens ?? 0,
        outputTokens: e.data.usage.output_tokens ?? 0,
      });
    if (e.type !== "agent_spawned") continue;
    const branch = await log.mainBranch(e.data.child_thread_id);
    if (branch.ok)
      totals = addTotals(
        totals,
        await treeTotals(store, {
          ...thread,
          id: e.data.child_thread_id,
          branch: branch.value,
        }),
      );
  }
  return totals;
}
