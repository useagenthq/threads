import {
  type BranchId,
  type KnownEvent,
  knownEvents,
  loopParked,
  openStore,
  type ParkAddress,
  type ThreadId,
  threadHandle,
} from "@threads/core/host";
import type { HostContext } from "../context";
import { outcomeFromLog, type RunOutcome } from "../outcome";
import { halted } from "../subscribe";
import type { Frame } from "./frame";
import type { LiveListener } from "./listener";
import { type SessionPlan, UiSession } from "./session";

// One UI stream of one run, from the log (spec/schema/ui/README.md): a UiSession fed the
// branch's log each time it may have changed and the live deltas this process's runs publish,
// until the run has an outcome. It reads the log and starts nothing.

export type StreamPlan = SessionPlan & {
  readonly tenant: string;
  readonly thread: { readonly id: ThreadId; readonly branch: BranchId };
};

/** A frame, or the end: `done` (the AI SDK stream then sends [DONE]) or `broken` (no [DONE]). */
export type Out = Frame | { readonly end: "done" | "broken" };

/**
 * The stream. `listener` was registered with the live hub before the run started or its head
 * was read; without live text (the cursor route) it only polls.
 */
export async function* uiFrames(
  ctx: HostContext,
  plan: StreamPlan,
  listener: LiveListener,
): AsyncGenerator<Out, void, undefined> {
  try {
    const run = await runLog(ctx, plan);
    const first = await run.read();
    if (first === undefined) return;
    const session = new UiSession(plan, first.events, listener.missed);
    yield* session.opening(first.events);
    for (;;) {
      yield* session.deltas(listener.take());
      const read = await run.read();
      if (read === undefined) return;
      const step = session.read(read.events);
      yield* step.frames;
      if (step.broken !== undefined) {
        console.debug(
          `threads host: live part ${step.broken} did not match its commit`,
        );
        yield { end: "broken" };
        return;
      }
      const outcome = run.outcome(read);
      if (outcome !== undefined) {
        yield* session.close(outcome);
        yield { end: "done" };
        return;
      }
      await listener.next();
    }
  } finally {
    listener.stop();
  }
}

type Read = {
  readonly events: readonly KnownEvent[];
  readonly parked: readonly ParkAddress[];
};

/** The branch's log as the stream reads it, and the run's outcome once it has one. */
async function runLog(
  ctx: HostContext,
  plan: StreamPlan,
): Promise<{
  readonly read: () => Promise<Read | undefined>;
  readonly outcome: (read: Read) => RunOutcome | undefined;
}> {
  const store = ctx.storeFor(plan.tenant);
  const { log } = await ctx.open(plan.tenant);
  const handle = threadHandle(await openStore(store), {
    ...plan.thread,
    store,
  });
  return {
    read: async () => {
      const r = await log.read(plan.thread.branch);
      return r.ok
        ? { events: knownEvents(r.value), parked: loopParked(r.value.fold) }
        : undefined;
    },
    outcome: (read) =>
      outcomeFromLog(read.events, plan.runId, read.parked, handle) ??
      halted(ctx, plan.runId),
  };
}
