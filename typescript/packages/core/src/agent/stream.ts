import type { RunInput, RunStream, StreamEvent } from "./agent";
import { type Resolved, type RunOptions, run } from "./run";

// stream(): a subscription to the run's log, plus its result. Not a second loop.

export function stream<Deps, Output>(
  def: Resolved<Deps, Output>,
  input: RunInput,
  options: RunOptions<Deps>,
): RunStream<Output> {
  const queue: StreamEvent[] = [];
  let wake: (() => void) | undefined;
  let done = false;
  const push = (item: StreamEvent): void => {
    queue.push(item);
    wake?.();
  };
  const result = run(def, input, options, {
    onEvent: (event) => push({ kind: "event", event }),
    onDelta: (id, text) => push({ kind: "delta", request_event_id: id, text }),
  });
  const finished = async (): Promise<void> => {
    try {
      await result;
    } catch {
      // The caller sees the failure through `result`; the iterator just ends.
    } finally {
      done = true;
      wake?.();
    }
  };
  void finished();
  return {
    result,
    async *[Symbol.asyncIterator]() {
      for (;;) {
        const next = queue.shift();
        if (next !== undefined) yield next;
        else if (done) return;
        else {
          const { promise, resolve } = Promise.withResolvers<void>();
          wake = resolve;
          await promise;
        }
      }
    },
  };
}
