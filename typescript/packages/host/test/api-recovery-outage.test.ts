import { afterEach, describe, expect, spyOn, test } from "bun:test";
import { type Model, scriptedModel, sqlite } from "@threads/core";
import { markTestKit } from "@threads/core/adapter";
import {
  knownEvents,
  openStore,
  StoreError,
  storeConnection,
  ThreadId,
  tenantStore,
  uuidv7,
} from "@threads/core/host";
import { hostTicked } from "../src/host";
import { cleanup, fold, serve, started } from "./api-kit";
import { alice, say, until } from "./kit";

// A store outage is the host's to wait out, never the model's to resend: one met while a model
// streams fails the run, which the host's recovery runs on with backoff; one met finding a
// channel thread's main branch backs off too, and the rest of the tick goes on.

afterEach(cleanup);

describe("a store outage", () => {
  test("met mid-stream fails the run, and the host runs it on after a wait, not the model", async () => {
    const store = sqlite(":memory:");
    const inner = scriptedModel({ responses: [say("done"), say("done")] });
    let sends = 0;
    const model: Model = {
      ...inner,
      send: async function* (
        ...[request, context, options]: Parameters<Model["send"]>
      ) {
        sends += 1;
        if (sends === 1) {
          yield { kind: "delta", text: "partial" };
          throw new StoreError("disk I/O error");
        }
        yield* inner.send(request, context, options);
      },
    };
    markTestKit(model);
    const logged = spyOn(console, "error");
    try {
      const h = serve(store, model);
      await h.ready();
      const run = await started(h, alice, "k-1");
      await until(
        async () =>
          !(await fold(store, alice.tenant, run.branch_id)).fold.turnOpen,
        8_000,
      );
      const events = knownEvents(
        await fold(store, alice.tenant, run.branch_id),
      );
      const reasons = events.flatMap((e) =>
        e.type === "model_attempt_abandoned" ? [e.data.reason] : [],
      );
      expect(reasons).not.toContain("stream_broken");
      expect(events.at(-1)?.type).toBe("turn_completed");
      const said = logged.mock.calls.map((c) => String(c[0]));
      expect(said.filter((m) => m.includes("store error"))).toHaveLength(1);
      expect(said.filter((m) => m.includes("failed"))).toHaveLength(0);
    } finally {
      logged.mockRestore();
    }
  }, 15_000);

  test("finding a channel thread's main branch backs off, and the tick's sweep still runs", async () => {
    const store = sqlite(":memory:");
    const { db } = await storeConnection(store);
    const thread = ThreadId.parse(uuidv7(Date.now()));
    db.run(
      `INSERT INTO channel_threads (tenant_id, channel, installation_id, address, thread_id)
        VALUES (?, 'slack', 'T1', 'C1', ?)`,
      [alice.tenant, thread],
    );
    const { log } = await openStore(tenantStore(store, alice.tenant));
    let lookups = 0;
    log.mainBranch = () => {
      lookups += 1;
      throw new StoreError("disk I/O error");
    };
    const all = db.all.bind(db);
    let sweeps = 0;
    const counted = (sql: string, params: Parameters<typeof db.all>[1]) => {
      if (sql.includes("FROM inbox WHERE consumed_seq IS NULL")) sweeps += 1;
      return all(sql, params);
    };
    Object.assign(db, { all: counted });
    const logged = spyOn(console, "error");
    try {
      const h = serve(store, scriptedModel({ responses: [] }));
      await h.ready();
      for (let i = 0; i < 6; i++) await hostTicked(h);
      const said = logged.mock.calls.map((c) => String(c[0]));
      // Backing off from 1 s: looked at 0, 1 and 3 s, not on every tick.
      expect(lookups).toBeLessThanOrEqual(3);
      expect(sweeps).toBeGreaterThanOrEqual(5);
      expect(said.filter((m) => m.includes("store error"))).toHaveLength(1);
      expect(said.filter((m) => m.includes("tick failed"))).toHaveLength(0);
    } finally {
      logged.mockRestore();
      Object.assign(db, { all });
    }
  }, 20_000);
});
