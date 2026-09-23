import { z } from "zod";
import type { KnownEvent, Policy, ToolSpec } from "../../src/log";
import type { LoopConfig, ToolImpl, ToolRun } from "../../src/loop";
import { type ScriptedModel, scriptedModel } from "../../src/model";
import { knownEvents } from "../../src/reduce";
import type { EventDraft, Writer } from "../../src/store";
import {
  type Fixture,
  fixture,
  ROOT,
  THREAD,
  unwrap,
  userInput,
} from "../store/helpers";

// A small branch-level harness for loop tests: a thread with the given tools, a scripted
// model, counted tool bodies and the injected clock.

export const EMAIL: ToolSpec = {
  name: "send_email",
  description: "Send an email.",
  input_schema: {
    type: "object",
    additionalProperties: false,
    required: ["to"],
    properties: { to: { type: "string" } },
  },
  effect_class: "unguarded",
};

export function startedWith(
  tools: readonly ToolSpec[],
  policy?: Policy,
): EventDraft {
  return {
    type: "thread_started",
    type_version: 1,
    critical: true,
    actor: { kind: "host" },
    data: {
      agent_name: "demo",
      config_hash: "a".repeat(64),
      model: { provider: "scripted", name: "scripted-1" },
      model_params: { max_tokens: 1024 },
      adapter: { name: "scripted", version: "1", settings: {} },
      instructions: "You are a helpful agent.",
      tools: [...tools],
      ...(policy === undefined ? {} : { policy }),
    },
  };
}

export type Harness = Fixture & {
  readonly model: ScriptedModel;
  readonly runs: Map<string, number>;
  readonly config: (overrides?: Partial<LoopConfig>) => LoopConfig;
};

/** A root branch holding `drafts`, released so the next acquire takes a new epoch. */
export function harness(
  tools: readonly ToolSpec[],
  drafts: readonly EventDraft[],
  responses: readonly unknown[],
  run: (name: string) => ToolRun = () => ({
    kind: "done",
    output: "ok",
    isError: false,
  }),
  policy?: Policy,
): Harness {
  const f = fixture();
  unwrap(f.store.createBranch(THREAD, ROOT));
  const writer = unwrap(f.store.acquire(ROOT, "setup", 1));
  unwrap(writer.append([startedWith(tools, policy), ...drafts]));
  f.clock.now += 10;
  const model = scriptedModel({ responses });
  const runs = new Map<string, number>();
  const impls = new Map<string, ToolImpl>(
    tools.map((spec) => [
      spec.name,
      {
        spec,
        input: z.record(z.string(), z.unknown()),
        run: async () => {
          runs.set(spec.name, (runs.get(spec.name) ?? 0) + 1);
          return run(spec.name);
        },
      },
    ]),
  );
  const config = (overrides: Partial<LoopConfig> = {}): LoopConfig => ({
    models: () => model,
    tools: impls,
    authorize: () => ({ decision: "allow", source: "policy" }),
    clock: {
      now: () => f.clock.now,
      sleepUntil: async (t) => {
        f.clock.now = Math.max(f.clock.now, t);
      },
    },
    principal: { issuer: "api", tenant: "acme", subject: "alice" },
    skewMarginMs: 1000,
    ...overrides,
  });
  return { ...f, model, runs, config };
}

export function events(writer: Writer): readonly KnownEvent[] {
  return knownEvents(writer.chain);
}

export { userInput };
