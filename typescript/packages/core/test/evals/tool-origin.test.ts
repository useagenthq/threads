import { describe, expect, test } from "bun:test";
import { z } from "zod";
import {
  agent,
  ConfigError,
  extension,
  scriptedModel,
  sqlite,
  tool,
} from "../../src";
import { openStore, storeOf } from "../../src/agent/sqlite";
import type { EventOf } from "../../src/fold/state";
import { knownEvents } from "../../src/reduce";
import { pinnedLine0 } from "../../src/render/prefix";
import { fixture, ROOT, THREAD, unwrap } from "../store/helpers";

// Tool origins (spec lane 22, test 4e): a pinned extension tool records the extension it came
// from. It isn't model-visible, so line 0 is byte-equal with and without it; config_hash covers
// it, so agents with extension tools get a new hash (pinned below, old and new), agents without
// keep theirs, and continuing an older thread says why it can't.

const say = {
  content: [{ type: "text", text: "hi" }],
  stop_reason: "end_turn",
  usage: { input_tokens: 1, output_tokens: 1 },
};
const note = tool({
  name: "note",
  description: "Write a note.",
  input: z.object({ text: z.string() }),
  effect: "read_only",
  execute: async ({ text }) => text,
});

/** Golden hashes: the plain agent's is the same before and after this release. */
const PLAIN_HASH =
  "30791ef9bc30dae79dd2371745c3be7c7122d9bd61c183d0171e066c583fe46a";
const EXTENSION_HASH_BEFORE =
  "8f3ab28b8b0c04aebf7201f18aecfa023d5ba7f7c9f1553bbbb9cbcfff4cfea9";
const EXTENSION_HASH_AFTER =
  "abd11f5f47cbe080d5cd6cc90275643d90a31407e8a0d2a15ad53478e43e0505";

const plain = () =>
  agent({
    name: "support",
    instructions: "Help.",
    model: scriptedModel({ responses: [say] }),
    tools: [note],
  });
const extended = (responses: readonly unknown[] = [say]) =>
  agent({
    name: "support",
    instructions: "Help.",
    model: scriptedModel({ responses }),
    extensions: [extension({ name: "audit", tools: [note] })],
  });

type Started = EventOf<"thread_started">["data"];

async function startedOf(bot: ReturnType<typeof plain>): Promise<Started> {
  const store = sqlite(":memory:");
  const run = await bot.run("Hi", { store });
  const { log } = await openStore(store);
  const first = knownEvents(unwrap(log.read(run.thread.branch)))[0];
  if (first?.type !== "thread_started") throw new Error("no thread_started");
  return first.data;
}

describe("tool origin", () => {
  test("an extension tool carries its origin; line 0 is the same without it", async () => {
    const started = await startedOf(extended());
    const tool = started.tools.find((t) => t.name === "audit__note");
    expect(tool?.origin).toEqual({ extension: "audit" });
    const bare = started.tools.map(({ origin: _, ...t }) => t);
    expect(pinnedLine0({ ...started, tools: bare })).toBe(pinnedLine0(started));
  });

  test("an agent without extension tools keeps its config_hash", async () => {
    expect((await startedOf(plain())).config_hash).toBe(PLAIN_HASH);
  });

  test("an agent with an extension tool gets a new config_hash, on purpose", async () => {
    const hash = (await startedOf(extended())).config_hash;
    expect(hash).toBe(EXTENSION_HASH_AFTER);
    expect(hash).not.toBe(EXTENSION_HASH_BEFORE);
  });

  test("continuing a thread started before origins says why it can't", async () => {
    const started = await startedOf(extended());
    const f = fixture();
    unwrap(f.store.createBranch(THREAD, ROOT));
    const writer = unwrap(f.store.acquire(ROOT, "earlier-release", 1));
    unwrap(
      writer.append([
        {
          type: "thread_started",
          type_version: 1,
          critical: true,
          actor: { kind: "host" },
          data: {
            ...started,
            config_hash: EXTENSION_HASH_BEFORE,
            tools: started.tools.map(({ origin: _, ...t }) => t),
          },
        },
      ]),
    );
    writer.release();
    f.clock.now += 60_000;
    const store = storeOf({ log: f.store, artifacts: f.artifacts });
    const error = await extended([say])
      .run("Again.", { store, thread: THREAD })
      .catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ConfigError);
    expect(
      error instanceof ConfigError ? [error.code, error.message] : [],
    ).toEqual([
      "invalid_config",
      "this thread was started with another config: tool origin was added in this release; start a new thread",
    ]);
  });
});
