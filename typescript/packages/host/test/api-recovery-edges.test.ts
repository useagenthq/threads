import { afterEach, describe, expect, spyOn, test } from "bun:test";
import { scriptedModel, sqlite } from "@threads/core";
import { storeConnection } from "@threads/core/host";
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
import { alice, eve, say, until } from "./kit";

// The edges of API run recovery: a corrupt receipt row is skipped and the valid ones still
// recover; a run that fails for a reason other than a held lease or a store error is not
// retried, stays open in the log and is logged with the reason.

afterEach(cleanup);

describe("API run recovery edges", () => {
  test("a corrupt receipt row is skipped and the valid ones recover", async () => {
    const store = sqlite(":memory:");
    const first = serve(store, stall());
    const good = await started(first, alice, "k-1");
    const bad = await started(first, eve, "k-2");
    await until(() =>
      has(store, alice.tenant, good.branch_id, "model_request"),
    );
    await until(() => has(store, eve.tenant, bad.branch_id, "model_request"));
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
      expect(String(skipped[0]?.[0])).toContain(bad.branch_id);
      expect(String(skipped[0]?.[0])).toContain("bad field thread_id");
      expect((await fold(store, eve.tenant, bad.branch_id)).fold.turnOpen).toBe(
        true,
      );
    } finally {
      logged.mockRestore();
    }
  }, 15_000);

  test("a run that fails another way is not retried and stays open", async () => {
    const store = sqlite(":memory:");
    const first = serve(store, stall());
    const run = await started(first, alice, "k-1");
    await until(() => has(store, alice.tenant, run.branch_id, "model_request"));
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
});
