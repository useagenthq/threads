import { describe, expect, test } from "bun:test";
import { storeOf } from "../../src/agent/sqlite";
import { SandboxId } from "../../src/log";
import { err, ok } from "../../src/result";
import {
  collect,
  type FakeSandbox,
  fakeSandbox,
  type Sandbox,
} from "../../src/sandbox";
import type { EventDraft } from "../../src/store";
import { openThread, recoverFork } from "../../src/thread";
import {
  CHILD,
  code,
  fixture,
  ROOT,
  snapshot,
  started,
  THREAD,
  turnCompleted,
  unwrap,
  userInput,
} from "../store/helpers";

// Crash and release paths of fork, driven step by step.

const LOST = {
  snapshots: {
    snap_01: {
      restore_sandbox_id: "sbx_child_01",
      restore_response: "lost",
      create_lookup: "found",
    },
  },
} as const;

function parent(snap: EventDraft = snapshot(null)) {
  const f = fixture();
  unwrap(f.store.createBranch(THREAD, ROOT));
  const writer = unwrap(f.store.acquire(ROOT, "holder-a"));
  unwrap(writer.append([started, userInput("hi"), turnCompleted, snap]));
  return f;
}

const request = { parent: ROOT, atSeq: 4, branch: CHILD, holderId: "creator" };
const LEASE = 30_000;

/** A fork that crashed after its ledger row, optionally after the provider call too. */
async function crashed(sandbox: FakeSandbox, called: boolean) {
  const f = parent();
  const writer = unwrap(f.store.beginFork(request));
  const row = unwrap(f.store.ledger.begin(writer, "sandbox", "fake"));
  if (called) await sandbox.restore("snap_01", row.operation_key);
  f.clock.now += LEASE + 1;
  return f;
}

/** A sandbox whose first close fails, as a provider outage would. */
function flakyClose(base: FakeSandbox): Sandbox {
  let failures = 1;
  return {
    ...base,
    attach: async (ref) => {
      const s = await base.attach(ref);
      if (!s.ok) return s;
      return ok({
        ...s.value,
        close: async () => {
          failures -= 1;
          return failures >= 0
            ? err({ code: "release_failed", message: "provider 500" })
            : s.value.close();
        },
      });
    },
  };
}

describe("a crash between the pending row and the create resolves by lookup", () => {
  test("the create never went out: a final not_found releases the row", async () => {
    const sandbox = fakeSandbox(LOST);
    const f = await crashed(sandbox, false);
    const states = unwrap(
      await recoverFork(f.store, sandbox, CHILD, "creator"),
    );
    expect(states).toEqual(["released"]);
    expect(unwrap(f.store.ledger.rows())[0]?.release_outcome).toBe(
      "not_created",
    );
    expect(unwrap(f.store.branchState(CHILD))).toBe("fork_failed");
    expect(sandbox.creates()).toBe(0);
  });

  test("the create went out: lookup finds the sandbox and it is released, not recreated", async () => {
    const sandbox = fakeSandbox(LOST);
    const f = await crashed(sandbox, true);
    const states = unwrap(
      await recoverFork(f.store, sandbox, CHILD, "creator"),
    );
    expect(states).toEqual(["released"]);
    expect(sandbox.creates()).toBe(1);
    expect((await sandbox.attach("sbx_child_01")).ok).toBe(false);
    const handle = await openThread(
      storeOf({ log: f.store, artifacts: f.artifacts }),
      THREAD,
      { branchId: CHILD },
    );
    expect(code(handle)).toBe("not_found");
  });

  test("an adapter that can't look it up parks the row as unknown", async () => {
    const sandbox = fakeSandbox({
      snapshots: {
        snap_01: { ...LOST.snapshots.snap_01, create_lookup: "unsupported" },
      },
    });
    const f = await crashed(sandbox, true);
    const states = unwrap(
      await recoverFork(f.store, sandbox, CHILD, "creator"),
    );
    expect(states).toEqual(["unknown"]);
    expect(sandbox.creates()).toBe(1);
  });

  test("nobody else reclaims a fork while its creator's lease is live", async () => {
    const sandbox = fakeSandbox(LOST);
    const f = parent();
    unwrap(f.store.beginFork(request));
    const busy = await recoverFork(f.store, sandbox, CHILD, "someone-else");
    expect(code(busy)).toBe("branch_busy");
  });
});

describe("a stale fork owner", () => {
  test("can't record a create, and its child never becomes visible", async () => {
    const f = parent();
    const stale = unwrap(f.store.beginFork(request));
    f.clock.now += LEASE + 1;
    unwrap(f.store.reclaimFork(CHILD, "next-owner"));
    expect(code(f.store.ledger.begin(stale, "sandbox", "fake"))).toBe(
      "stale_epoch",
    );
    const finished = f.store.finishFork(stale, {
      sandboxId: SandboxId.parse("sbx_child_01"),
      knowledgePolicy: "pinned",
    });
    expect(code(finished)).toBe("writer_poisoned");
    expect(unwrap(f.store.branchState(CHILD))).toBe("forking");
  });
});

describe("a failed release is retried, never dropped", () => {
  test("release_failed stays until gc releases it", async () => {
    const base = fakeSandbox({
      snapshots: { snap_01: { restore_sandbox_id: "sbx_child_01" } },
    });
    const sandbox = flakyClose(base);
    const f = parent();
    const writer = unwrap(f.store.beginFork(request));
    const row = unwrap(f.store.ledger.begin(writer, "sandbox", "fake"));
    const made = unwrap(await sandbox.restore("snap_01", row.operation_key));
    unwrap(f.store.ledger.live(writer, row.resource_id, made.id, null));
    f.clock.now += LEASE + 1; // crash before the fork event

    const states = unwrap(
      await recoverFork(f.store, sandbox, CHILD, "creator"),
    );
    expect(states).toEqual(["release_failed"]);
    expect(unwrap(f.store.ledger.rows())[0]?.release_outcome).toBe(
      "provider 500",
    );
    expect(unwrap(await collect(f.store.ledger, sandbox))).toEqual([]); // creator still holds
    f.clock.now += LEASE + 1;
    const done = unwrap(await collect(f.store.ledger, sandbox));
    expect(done.map((r) => [r.state, r.release_outcome])).toEqual([
      ["released", "released"],
    ]);
    expect((await base.attach("sbx_child_01")).ok).toBe(false);
  });
});
