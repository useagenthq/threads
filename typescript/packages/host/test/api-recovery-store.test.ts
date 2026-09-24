import { afterEach, describe, expect, spyOn, test } from "bun:test";
import { AssertionError } from "node:assert";
import {
  existsSync,
  mkdtempSync,
  renameSync,
  rmSync,
  unlinkSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  agent,
  extension,
  type Model,
  type Store,
  scriptedModel,
  sqlite,
} from "@threads/core";
import { markTestKit } from "@threads/core/adapter";
import {
  knownEvents,
  openStore,
  StoreError,
  tenantStore,
} from "@threads/core/host";
import type { RunAccepted } from "../src";
import { hostTicked } from "../src/host";
import {
  cleanup,
  expireLeases,
  fold,
  has,
  serve,
  serveAgent,
  stall,
  started,
} from "./api-kit";
import { alice, say, until } from "./kit";

// What a host retries when it recovers an API run: only a store error, which the store raises
// as StoreError, backing off while it lasts. A provider's lookup failing is the loop's to
// settle (a model request becomes unknown and is sent again under the loop's own budget); the
// host never retries it.

afterEach(cleanup);

/** A model with response lookup whose lookup always throws `fault()`. */
function lookingUp(
  fault: () => Error,
): Model & { readonly lookups: () => number } {
  const model = scriptedModel({ responses: [say("done")], lookup: {} });
  let lookups = 0;
  const made = {
    ...model,
    lookups: () => lookups,
    lookup: async () => {
      lookups += 1;
      throw fault();
    },
  };
  markTestKit(made);
  return made;
}

const refused = (): Error =>
  Object.assign(new Error("connect ECONNREFUSED"), { code: "ECONNREFUSED" });

/** A stalled host's API run, its model request open and its lease run out. */
async function crashedRun(store: Store): Promise<RunAccepted> {
  const first = serve(
    store,
    stall(scriptedModel({ responses: [say("late")], lookup: {} })),
  );
  const run = await started(first, alice, "k-1");
  await until(() => has(store, alice.tenant, run.branch_id, "model_request"));
  await expireLeases(store);
  return run;
}

/** A host whose runs wait for `held` once their input is durable, before the model request. */
function gatedHost(
  store: Store,
  held: Promise<void>,
  reached: () => void,
): ReturnType<typeof serve> {
  const gate = extension({
    name: "gate",
    hooks: {
      beforeModel: async () => {
        reached();
        await held;
        return { decision: "proceed" };
      },
    },
  });
  return serveAgent(
    store,
    agent({
      name: "support",
      model: scriptedModel({ responses: [say("done")] }),
      extensions: [gate],
    }),
  );
}

const closed = async (store: Store, run: RunAccepted): Promise<boolean> =>
  !(await fold(store, alice.tenant, run.branch_id)).fold.turnOpen;

describe("what API run recovery retries", () => {
  test("a provider lookup that throws is settled by the loop, never retried by the host", async () => {
    const store = sqlite(":memory:");
    const first = serve(
      store,
      stall(scriptedModel({ responses: [say("late")], lookup: {} })),
    );
    const run = await started(first, alice, "k-1");
    await until(() => has(store, alice.tenant, run.branch_id, "model_request"));
    await expireLeases(store);

    const model = lookingUp(refused);
    const second = serve(store, model);
    await second.ready();
    await until(
      async () =>
        !(await fold(store, alice.tenant, run.branch_id)).fold.turnOpen,
      5_000,
    );
    await hostTicked(second);
    await hostTicked(second);
    expect(model.lookups()).toBe(1);
    const events = knownEvents(await fold(store, alice.tenant, run.branch_id));
    const abandoned = events.find((e) => e.type === "model_attempt_abandoned");
    expect(abandoned?.data).toMatchObject({ provider_outcome: "unknown" });
    expect(events.at(-1)?.type).toBe("turn_completed");
  }, 15_000);

  test("a lasting store error backs off, is said once, and the run goes on when it passes", async () => {
    const store = sqlite(":memory:");
    const first = serve(store, stall());
    const run = await started(first, alice, "k-1");
    await until(() => has(store, alice.tenant, run.branch_id, "model_request"));

    const second = serve(store, scriptedModel({ responses: [say("done")] }));
    await second.ready();
    // The first pass loses to the live lease; from then on every run's acquire fails.
    await hostTicked(second);
    await hostTicked(second);
    const { log } = await openStore(tenantStore(store, alice.tenant));
    const acquire = log.acquire.bind(log);
    let failing = true;
    let attempts = 0;
    log.acquire = (branch, holder, ttl) => {
      if (!failing || !holder.startsWith("run-"))
        return acquire(branch, holder, ttl);
      attempts += 1;
      throw new StoreError("disk I/O error");
    };
    await expireLeases(store);
    const logged = spyOn(console, "error");
    try {
      for (let i = 0; i < 6; i++) await hostTicked(second);
      // Once a second would be 6; backing off from 1 s it is at most 3 (at 0, 1 and 3 s).
      expect(attempts).toBeGreaterThanOrEqual(2);
      expect(attempts).toBeLessThanOrEqual(3);
      const said = logged.mock.calls.map((c) => String(c[0]));
      expect(said.filter((m) => m.includes("store error"))).toHaveLength(1);
      expect(said.filter((m) => m.includes("failed"))).toHaveLength(0);
    } finally {
      logged.mockRestore();
    }
    failing = false;
    await until(
      async () =>
        !(await fold(store, alice.tenant, run.branch_id)).fold.turnOpen,
      12_000,
    );
  }, 30_000);

  test("a lookup that fails an assertion is a bug: the run fails and the host doesn't retry it", async () => {
    const store = sqlite(":memory:");
    const run = await crashedRun(store);
    const model = lookingUp(
      () => new AssertionError({ message: "lookup broke" }),
    );
    const logged = spyOn(console, "error");
    try {
      const second = serve(store, model);
      await second.ready();
      for (let i = 0; i < 4; i++) await hostTicked(second);
      const said = logged.mock.calls.map((c) => String(c[0]));
      expect(said.filter((m) => m.includes("not retried"))).toHaveLength(1);
      expect(model.lookups()).toBe(1);
      expect(await closed(store, run)).toBe(false);
    } finally {
      logged.mockRestore();
    }
  }, 15_000);

  test("an artifact write that fails is a store error, and the same host completes the turn once it passes", async () => {
    const dir = mkdtempSync(join(tmpdir(), "threads-artifacts-"));
    try {
      const store = sqlite(dir);
      // A host that stalls once its input is durable, before its model request.
      const held = Promise.withResolvers<void>();
      const reached = Promise.withResolvers<void>();
      const first = gatedHost(store, held.promise, reached.resolve);
      const run = await started(first, alice, "k-1");
      await reached.promise;
      await expireLeases(store);
      // The artifact directory is replaced by a file: every artifact read and write fails.
      const artifacts = join(dir, "artifacts");
      const had = existsSync(artifacts);
      if (had) renameSync(artifacts, `${artifacts}.away`);
      writeFileSync(artifacts, "");
      const logged = spyOn(console, "error");
      const second = gatedHost(store, Promise.resolve(), () => {});
      try {
        await second.ready();
        for (let i = 0; i < 3; i++) await hostTicked(second);
        const said = logged.mock.calls.map((c) => String(c[0]));
        expect(said.filter((m) => m.includes("store error"))).toHaveLength(1);
        expect(said.filter((m) => m.includes("not retried"))).toHaveLength(0);
        expect(await closed(store, run)).toBe(false);
      } finally {
        logged.mockRestore();
        unlinkSync(artifacts);
        if (had) renameSync(`${artifacts}.away`, artifacts);
      }
      await until(() => closed(store, run), 12_000);
      held.resolve();
    } finally {
      await cleanup();
      rmSync(dir, { recursive: true, force: true });
    }
  }, 30_000);

  test("a store error reading the branch backs off too, and the tick goes on", async () => {
    const store = sqlite(":memory:");
    const run = await crashedRun(store);
    const { log } = await openStore(tenantStore(store, alice.tenant));
    const read = log.read.bind(log);
    let failing = true;
    let reads = 0;
    log.read = (branch) => {
      if (!failing || branch !== run.branch_id) return read(branch);
      reads += 1;
      throw new StoreError("disk I/O error");
    };
    const logged = spyOn(console, "error");
    const second = serve(store, scriptedModel({ responses: [say("done")] }));
    try {
      await second.ready();
      for (let i = 0; i < 6; i++) await hostTicked(second);
      const said = logged.mock.calls.map((c) => String(c[0]));
      expect(reads).toBeLessThanOrEqual(3);
      expect(said.filter((m) => m.includes("store error"))).toHaveLength(1);
      expect(said.filter((m) => m.includes("tick failed"))).toHaveLength(0);
    } finally {
      logged.mockRestore();
    }
    failing = false;
    await until(() => closed(store, run), 12_000);
  }, 30_000);
});
