import { describe, expect, test } from "bun:test";
import {
  collect,
  type FakeSandbox,
  fakeSandbox,
  manifestHash,
  ownerContext,
  type Sandbox,
} from "../../src/sandbox";
import type { EventDraft } from "../../src/store";
import { recoverFork } from "../../src/thread";
import { forkBranch } from "../../src/thread/fork";
import { CTX } from "../sandbox/context";
import {
  CHILD,
  code,
  fixture,
  ROOT,
  started,
  THREAD,
  turnCompleted,
  unwrap,
  userInput,
} from "../store/helpers";

// every provider operation is fenced at the adapter's dispatch point, by
// the owner's lease or by gc's cleanup claim, and a row is resolved only by its own provider.

const LEASE = 30_000;
const SCRIPT = {
  snapshots: { snap_01: { restore_sandbox_id: "sbx_child_01", manifest: [] } },
};

function parent() {
  const f = fixture();
  unwrap(f.store.createBranch(THREAD, ROOT));
  const writer = unwrap(f.store.acquire(ROOT, "holder-a"));
  const snapshot: EventDraft = {
    type: "snapshot",
    type_version: 1,
    critical: true,
    actor: { kind: "host" },
    data: {
      snapshot_id: "snap_01",
      provider: "fake",
      sandbox_id: "sbx_parent_01",
      capture_class: "filesystem",
      expires_at: null,
      manifest_hash: manifestHash([]),
      quiesced: { frozen: [], stopped: [], excluded: [] },
    },
  };
  unwrap(writer.append([started, userInput("hi"), turnCompleted, snapshot]));
  return f;
}

/** Holds a call until released: a dispatch queued before it reaches the provider. */
function gate() {
  const open = Promise.withResolvers<void>();
  const entered = Promise.withResolvers<void>();
  return {
    hold: async (): Promise<void> => {
      entered.resolve();
      await open.promise;
    },
    entered: entered.promise,
    release: () => open.resolve(),
  };
}

function gatedRestore(base: FakeSandbox, g: ReturnType<typeof gate>): Sandbox {
  return {
    ...base,
    restore: async (id, hash, key, context) => {
      await g.hold();
      return base.restore(id, hash, key, context);
    },
  };
}

function gatedAttach(base: FakeSandbox, g: ReturnType<typeof gate>): Sandbox {
  return {
    ...base,
    attach: async (ref, context) => {
      await g.hold();
      return base.attach(ref, context);
    },
  };
}

const request = {
  parent: ROOT,
  child: CHILD,
  knowledge: "pinned",
  holderId: "creator",
} as const;

describe("a stale restore can't escape recovery", () => {
  test("the creator loses its lease while queued: recovery retires the row, the late restore creates nothing", async () => {
    const f = parent();
    const base = fakeSandbox(SCRIPT);
    const g = gate();
    const log = unwrap(f.store.read(ROOT));
    const point = log.events.at(-1)?.event.event_id;
    if (point === undefined)
      throw new Error("the parent ends with its snapshot");
    const forking = forkBranch(f.store, gatedRestore(base, g), {
      ...request,
      point,
    });
    await g.entered;

    f.clock.now += LEASE + 1; // the creator stalls; its lease lapses
    const states = unwrap(await recoverFork(f.store, base, CHILD, "restarted"));
    expect(states).toEqual(["released"]); // final not_found: nothing was created yet

    g.release();
    expect(code(await forking)).toBe("stale_epoch");
    expect(base.creates()).toBe(0);
    expect((await base.attach("sbx_child_01", CTX)).ok).toBe(false);
    expect(unwrap(f.store.branchState(CHILD))).toBe("fork_failed");
  });
});

describe("only the row's provider can resolve it", () => {
  test("another provider's adapter is refused before lookup; the row waits for the right one", async () => {
    const f = parent();
    const fake = fakeSandbox(SCRIPT);
    const other: Sandbox = {
      ...fakeSandbox(SCRIPT),
      info: { ...fake.info, provider: "other" },
    };
    const writer = unwrap(
      f.store.beginFork({
        parent: ROOT,
        atSeq: 4,
        branch: CHILD,
        holderId: "c",
      }),
    );
    const row = unwrap(f.store.ledger.begin(writer, "sandbox", "fake"));
    unwrap(
      await fake.restore(
        "snap_01",
        manifestHash([]),
        row.operation_key,
        ownerContext(writer),
      ),
    );
    f.clock.now += LEASE + 1;

    expect(code(await recoverFork(f.store, other, CHILD, "r"))).toBe(
      "invalid_request",
    );
    expect(unwrap(f.store.ledger.rows()).map((r) => r.state)).toEqual([
      "pending",
    ]);
    expect(unwrap(f.store.branchState(CHILD))).toBe("forking");

    expect(unwrap(await recoverFork(f.store, fake, CHILD, "r"))).toEqual([
      "released",
    ]);
    expect((await fake.attach("sbx_child_01", ownerContext(writer))).ok).toBe(
      false,
    );
  });
});

describe("gc works under a cleanup claim", () => {
  /** A live child sandbox row its owner has marked releasing, as deleting the thread does. */
  async function releasing() {
    const f = parent();
    const fake = fakeSandbox(SCRIPT);
    const writer = unwrap(
      f.store.beginFork({
        parent: ROOT,
        atSeq: 4,
        branch: CHILD,
        holderId: "c",
      }),
    );
    const row = unwrap(f.store.ledger.begin(writer, "sandbox", "fake"));
    const made = unwrap(
      await fake.restore(
        "snap_01",
        manifestHash([]),
        row.operation_key,
        ownerContext(writer),
      ),
    );
    unwrap(f.store.ledger.live(writer, row.resource_id, made.id, null));
    unwrap(f.store.ledger.releasing(writer, row.resource_id));
    return { f, fake };
  }

  test("after the owning branch is deleted, gc still releases its resource", async () => {
    const { f, fake } = await releasing();
    for (const table of ["events", "leases", "branches"])
      f.db.run(`DELETE FROM ${table} WHERE branch_id = ?`, [CHILD]);
    const done = unwrap(await collect(f.store.ledger, fake));
    expect(done.map((r) => [r.state, r.release_outcome])).toEqual([
      ["released", "released"],
    ]);
    expect((await fake.attach("sbx_child_01", CTX)).ok).toBe(false);
  });

  test("of two concurrent gc runs, only the latest claim holder dispatches", async () => {
    const { f, fake } = await releasing();
    f.clock.now += LEASE + 1;
    const g = gate();
    const first = collect(f.store.ledger, gatedAttach(fake, g));
    await g.entered; // run A claimed the row and is queued before the provider
    const second = unwrap(await collect(f.store.ledger, fake)); // run B claims it again
    expect(second.map((r) => r.state)).toEqual(["released"]);
    g.release();
    expect(unwrap(await first)).toEqual([]); // A's fence fails: it dispatches and records nothing
    expect(unwrap(f.store.ledger.rows()).map((r) => r.release_outcome)).toEqual(
      ["released"],
    );
  });
});
