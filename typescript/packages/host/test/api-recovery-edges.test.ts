import { afterEach, describe, expect, spyOn, test } from "bun:test";
import { agent, type Model, scriptedModel, sqlite } from "@threads/core";
import { markTestKit } from "@threads/core/adapter";
import {
  knownEvents,
  openStore,
  type Principal,
  storeConnection,
  tenantStore,
} from "@threads/core/host";
import { type Host, host } from "../src";
import { hostTicked } from "../src/host";
import { alice, authenticate, eve, say, until } from "./kit";

// The edges of API run recovery: a corrupt receipt row is skipped and the valid ones still
// recover; a run that fails for a reason other than a held lease is not retried, stays open in
// the log and is logged with the reason.

const hosts: Host[] = [];
const release: (() => void)[] = [];
afterEach(async () => {
  for (const open of release.splice(0)) open();
  for (const h of hosts.splice(0)) await h.stop();
});

/** A model whose requests never answer until the test ends: its host is as good as dead. */
function stalled(): Model {
  const model = scriptedModel({ responses: [say("late"), say("late")] });
  const { promise, resolve } = Promise.withResolvers<void>();
  release.push(resolve);
  const made: Model = {
    ...model,
    send: async function* (
      ...[request, context, options]: Parameters<Model["send"]>
    ) {
      await promise;
      yield* model.send(request, context, options);
    },
  };
  markTestKit(made);
  return made;
}

function serve(
  store: ReturnType<typeof sqlite>,
  model: Model,
  instructions?: string,
): Host {
  const support = agent({
    name: "support",
    model,
    ...(instructions === undefined ? {} : { instructions }),
  });
  const h = host({ store, authenticate, agents: { support } });
  hosts.push(h);
  return h;
}

async function started(h: Host, as: Principal, key: string) {
  const run = await h.startRun(
    { agent: "support", input: "Hello" },
    { principal: as, idempotencyKey: key },
  );
  if (!run.ok) throw new Error(run.error.message);
  return run.value;
}

async function fold(
  store: ReturnType<typeof sqlite>,
  tenant: string,
  branch: Parameters<Awaited<ReturnType<typeof openStore>>["log"]["read"]>[0],
) {
  const { log } = await openStore(tenantStore(store, tenant));
  const read = log.read(branch);
  if (!read.ok) throw new Error(read.error.message);
  return read.value;
}

const requested = async (
  store: ReturnType<typeof sqlite>,
  tenant: string,
  branch: Parameters<typeof fold>[2],
): Promise<boolean> =>
  knownEvents(await fold(store, tenant, branch)).some(
    (e) => e.type === "model_request",
  );

async function expireLeases(store: ReturnType<typeof sqlite>): Promise<void> {
  const { db } = await storeConnection(store);
  db.run("UPDATE leases SET expires_at = 0", []);
}

describe("API run recovery edges", () => {
  test("a corrupt receipt row is skipped and the valid ones recover", async () => {
    const store = sqlite(":memory:");
    const first = serve(store, stalled());
    const good = await started(first, alice, "k-1");
    const bad = await started(first, eve, "k-2");
    await until(() => requested(store, alice.tenant, good.branch_id));
    await until(() => requested(store, eve.tenant, bad.branch_id));
    const { db } = await storeConnection(store);
    db.run(
      "UPDATE run_receipts SET thread_id = 'not-a-uuid' WHERE branch_id = ?",
      [bad.branch_id],
    );
    await expireLeases(store);

    const logged = spyOn(console, "error");
    try {
      const second = serve(store, scriptedModel({ responses: [say("done")] }));
      await second.ready();
      await until(
        async () =>
          !(await fold(store, alice.tenant, good.branch_id)).fold.turnOpen,
        5_000,
      );
      await hostTicked(second);
      const skipped = logged.mock.calls.filter((c) =>
        String(c[0]).includes("run_receipts"),
      );
      expect(skipped).toHaveLength(1);
      expect(JSON.stringify(skipped[0])).toContain(bad.branch_id);
      expect((await fold(store, eve.tenant, bad.branch_id)).fold.turnOpen).toBe(
        true,
      );
    } finally {
      logged.mockRestore();
    }
  }, 15_000);

  test("a run that fails another way is not retried and stays open", async () => {
    const store = sqlite(":memory:");
    const first = serve(store, stalled());
    const run = await started(first, alice, "k-1");
    await until(() => requested(store, alice.tenant, run.branch_id));
    await expireLeases(store);

    const logged = spyOn(console, "error");
    try {
      // The host was redeployed with another config: the open run can't continue here.
      const changed = serve(
        store,
        scriptedModel({ responses: [say("never")] }),
        "Be brief.",
      );
      await changed.ready();
      for (let i = 0; i < 4; i++) await hostTicked(changed);
      const gaveUp = logged.mock.calls.filter((c) =>
        String(c[0]).includes("not retried"),
      );
      expect(gaveUp).toHaveLength(1);
      expect(JSON.stringify(gaveUp[0])).toContain("another config");
      const attempts = logged.mock.calls.filter((c) =>
        String(c[0]).includes("failed"),
      );
      expect(attempts).toHaveLength(1);
      expect(
        (await fold(store, alice.tenant, run.branch_id)).fold.turnOpen,
      ).toBe(true);
    } finally {
      logged.mockRestore();
    }
  }, 15_000);

  test("a store error inside a resumed run is retried and the turn completes", async () => {
    const store = sqlite(":memory:");
    const first = serve(store, stalled());
    const run = await started(first, alice, "k-1");
    await until(() => requested(store, alice.tenant, run.branch_id));

    const second = serve(store, scriptedModel({ responses: [say("done")] }));
    await second.ready();
    // The first pass loses to the live lease; the next run's acquire hits a store error once.
    await hostTicked(second);
    await hostTicked(second);
    const { log } = await openStore(tenantStore(store, alice.tenant));
    const acquire = log.acquire.bind(log);
    let blinked = false;
    log.acquire = (branch, holder, ttl) => {
      if (blinked || !holder.startsWith("run-"))
        return acquire(branch, holder, ttl);
      blinked = true;
      throw Object.assign(new Error("database is locked"), {
        name: "SQLiteError",
        code: "SQLITE_BUSY",
      });
    };
    await until(async () => blinked, 5_000);
    await expireLeases(store);
    await until(
      async () =>
        !(await fold(store, alice.tenant, run.branch_id)).fold.turnOpen,
      5_000,
    );
  }, 15_000);
});
