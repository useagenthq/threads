import { expect, test } from "bun:test";
import {
  type Agent,
  agent,
  type Model,
  ModelBlockedError,
  sqlite,
} from "@threads/core";
import {
  BranchId,
  dryPin,
  openStore,
  pinnedLine0,
  ThreadStartedData,
} from "@threads/core/host";

// Lane 22's dry-pin cases, shared by every first-party model adapter (spec 22, test 4d): the pin
// `threads eval --agent` makes without setup, secrets or MCP must be byte-equal, in line 0 and
// config_hash, to the one a real run pins after setup. The real run is stopped by the test
// model-request guard before any request, after thread_started is durable.

export type DryPinCase = {
  readonly factory: string;
  readonly env: string;
  /** The adapter under test, built with a fetch that fails any request. */
  readonly model: () => Model;
};

/** The thread_started a real run pins after setup; the guard stops it before a request. */
async function realPin(bot: Agent) {
  const store = sqlite(":memory:");
  const run = bot.stream("Hi", { store });
  let branch: string | undefined;
  for await (const item of run)
    if (item.kind === "event") branch ??= item.event.branch_id;
  await expect(run.result).rejects.toBeInstanceOf(ModelBlockedError);
  const { log } = await openStore(store);
  const read = await log.read(BranchId.parse(branch));
  if (!read.ok) throw new Error(read.error.message);
  const first = read.value.events[0]?.event;
  if (first?.type !== "thread_started") throw new Error("no thread_started");
  return ThreadStartedData.parse(first.data);
}

export function dryPinCases(c: DryPinCase): void {
  test(`${c.factory}: the dry pin's line 0 and config_hash equal the real pin's`, async () => {
    process.env[c.env] = "dummy-key-for-the-real-pin";
    try {
      const bot = agent({
        name: "support",
        model: c.model(),
        instructions: "Help.",
      });
      const real = await realPin(bot);
      const dry = dryPin(bot).started;
      expect(pinnedLine0(dry)).toBe(pinnedLine0(real));
      expect(dry.config_hash).toBe(real.config_hash);
    } finally {
      delete process.env[c.env];
    }
  });

  test(`${c.factory}: the dry pin reads no key: line 0 is the same with and without one`, () => {
    delete process.env[c.env];
    const bot = agent({ name: "support", model: c.model() });
    const without = pinnedLine0(dryPin(bot).started);
    process.env[c.env] = "dummy-key";
    try {
      expect(pinnedLine0(dryPin(bot).started)).toBe(without);
    } finally {
      delete process.env[c.env];
    }
  });
}
