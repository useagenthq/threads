import { afterEach, describe, expect, spyOn, test } from "bun:test";
import { type Model, scriptedModel, sqlite } from "@threads/core";
import { markTestKit } from "@threads/core/adapter";
import {
  knownEvents,
  openStore,
  StoreError,
  tenantStore,
} from "@threads/core/host";
import { hostTicked } from "../src/host";
import {
  cleanup,
  expireLeases,
  fold,
  has,
  serve,
  stall,
  started,
} from "./api-kit";
import { alice, say, until } from "./kit";

// What a host retries when it recovers an API run: only a store error, which the store raises
// as StoreError, backing off while it lasts. A provider's lookup failing is the loop's to
// settle (a model request becomes unknown and is sent again under the loop's own budget); the
// host never retries it.

afterEach(cleanup);

/** A model with response lookup whose lookup always fails as a refused connection. */
function refusing(): Model & { readonly lookups: () => number } {
  const model = scriptedModel({ responses: [say("done")], lookup: {} });
  let lookups = 0;
  const made = {
    ...model,
    lookups: () => lookups,
    lookup: async () => {
      lookups += 1;
      throw Object.assign(new Error("connect ECONNREFUSED"), {
        code: "ECONNREFUSED",
      });
    },
  };
  markTestKit(made);
  return made;
}

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

    const model = refusing();
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
});
