import {
  type Agent,
  agent,
  type Model,
  scriptedModel,
  type sqlite,
  type ThreadRef,
} from "../../src";
import { openStore } from "../../src/agent/sqlite";
import type { KnownEvent, Principal } from "../../src/log";
import { markTestKit } from "../../src/model/guard";
import { knownEvents } from "../../src/reduce";
import { unwrap } from "../store/helpers";

// Shared pieces for the background-wake tests: scripted answers, a model gated on a promise,
// and reads of the lead's log.

export const usage = { input_tokens: 10, output_tokens: 2 } as const;
export const say = (text: string): unknown => ({
  content: [{ type: "text", text }],
  stop_reason: "end_turn",
  usage,
});
export const scan = (id: string, agentName = "scanner"): unknown => ({
  content: [
    {
      type: "tool_use",
      call_id: id,
      name: "spawn_agent",
      input: { agent: agentName, prompt: "Scan.", background: true },
    },
  ],
  stop_reason: "tool_use",
  usage,
});
export const alice: Principal = {
  issuer: "api",
  tenant: "local",
  subject: "alice",
};

/** A scripted model whose every answer waits for `open`. */
export function gated(
  responses: readonly unknown[],
  open: Promise<void>,
): Model {
  const model = scriptedModel({ responses: [...responses] });
  const made: Model = {
    ...model,
    send: async function* (...args: Parameters<Model["send"]>) {
      await open;
      yield* model.send(...args);
    },
  };
  markTestKit(made);
  return made;
}

export function lead(
  scanner: Agent<never, unknown>,
  responses: readonly unknown[],
): Agent<undefined, string> {
  return agent({
    name: "lead",
    model: scriptedModel({ responses: [...responses] }),
    subagents: [scanner],
  });
}

export async function events(
  store: ReturnType<typeof sqlite>,
  thread: ThreadRef,
): Promise<readonly KnownEvent[]> {
  const { log } = await openStore(store);
  return knownEvents(unwrap(await log.read(thread.branch)));
}

/** Every late result is followed, after its append's block, by a woken naming the block. */
export function wakes(
  log: readonly KnownEvent[],
): readonly (readonly string[])[] {
  return log.flatMap((e) => (e.type === "woken" ? [e.data.causes] : []));
}

export function lateIds(log: readonly KnownEvent[]): readonly string[] {
  return log.flatMap((e) =>
    e.type === "tool_result_late" ? [e.event_id] : [],
  );
}

/** `model`, counting its requests and settling `entered` on the first. */
export function watched(model: Model): {
  readonly model: Model;
  readonly calls: () => number;
  readonly entered: Promise<void>;
} {
  let calls = 0;
  const entered = Promise.withResolvers<void>();
  const made: Model = {
    ...model,
    send: (...args: Parameters<Model["send"]>) => {
      calls += 1;
      entered.resolve();
      return model.send(...args);
    },
  };
  markTestKit(made);
  return { model: made, calls: () => calls, entered: entered.promise };
}
