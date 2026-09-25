import { describe, expect, test } from "bun:test";
import {
  collect,
  manifestHash,
  ownerContext,
  type Sandbox,
} from "../../src/sandbox";
import type { EventDraft } from "../../src/store";
import { recoverFork } from "../../src/thread";
import { forkBranch } from "../../src/thread/fork";
import {
  CHILD,
  code,
  fixture,
  ROOT,
  run,
  started,
  THREAD,
  turnCompleted,
  unwrap,
  userInput,
} from "../store/helpers";
import { CTX } from "./context";
import type { SandboxHarness } from "./harness";

// every provider operation is fenced at the adapter's dispatch point, by
// the owner's lease or by gc's cleanup claim, and a row is resolved only by its own provider.
// This crash and takeover suite runs against every adapter (ledgerSuite).

const LEASE = 30_000;
const SCRIPT = {
  snapshots: { snap_01: { restore_sandbox_id: "sbx_child_01", manifest: [] } },
};

async function parent() {
  const f = await fixture();
  unwrap(await f.store.createBranch(THREAD, ROOT));
  const writer = unwrap(await f.store.acquire(ROOT, "holder-a"));
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
  unwrap(
    await writer.append([started, userInput("hi"), turnCompleted, snapshot]),
  );
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

function gatedRestore(base: Sandbox, g: ReturnType<typeof gate>): Sandbox {
  return {
    ...base,
    restore: async (id, hash, key, context) => {
      await g.hold();
      return base.restore(id, hash, key, context);
    },
  };
}

function gatedAttach(base: Sandbox, g: ReturnType<typeof gate>): Sandbox {
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

/** The ledger crash and takeover suite against one adapter. */
export function ledgerSuite(harness: SandboxHarness): void {
  // A final lookup retires a never-created row as released; a nonfinal one can only park it.
  const retired = (sandbox: Sandbox): "released" | "unknown" =>
    sandbox.info.lookup.create === "final" ? "released" : "unknown";

  describe(`ledger crash and takeover: ${harness.name}`, () => {
    describe("a stale restore can't escape recovery", () => {
      test("the creator loses its lease while queued: recovery retires the row, the late restore creates nothing", async () => {
        const f = await parent();
        const { sandbox: base, creates } = harness.make(SCRIPT);
        const g = gate();
        const log = unwrap(await f.store.read(ROOT));
        const point = log.events.at(-1)?.event.event_id;
        if (point === undefined)
          throw new Error("the parent ends with its snapshot");
        const forking = forkBranch(f.store, gatedRestore(base, g), {
          ...request,
          point,
        });
        await g.entered;

        f.clock.now += LEASE + 1; // the creator stalls; its lease lapses
        const states = unwrap(
          await recoverFork(f.store, base, CHILD, "restarted"),
        );
        expect(states).toEqual([retired(base)]); // not_found: nothing was created yet

        g.release();
        expect(code(await forking)).toBe("stale_epoch");
        expect(creates()).toBe(0);
        expect((await base.attach("sbx_child_01", CTX)).ok).toBe(false);
        expect(unwrap(await f.store.branchState(CHILD))).toBe("fork_failed");
      });
    });

    describe("only the row's provider can resolve it", () => {
      test("another provider's adapter is refused before lookup; the row waits for the right one", async () => {
        const f = await parent();
        const fake = harness.make(SCRIPT).sandbox;
        const other: Sandbox = {
          ...harness.make(SCRIPT).sandbox,
          info: { ...fake.info, provider: "other" },
        };
        const writer = unwrap(
          await f.store.beginFork({
            parent: ROOT,
            atSeq: 4,
            branch: CHILD,
            holderId: "c",
          }),
        );
        const row = unwrap(
          await f.store.ledger.begin(writer, "sandbox", "fake"),
        );
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
        expect(unwrap(await f.store.ledger.rows()).map((r) => r.state)).toEqual(
          ["pending"],
        );
        expect(unwrap(await f.store.branchState(CHILD))).toBe("forking");

        expect(unwrap(await recoverFork(f.store, fake, CHILD, "r"))).toEqual([
          "released",
        ]);
        expect(
          (await fake.attach("sbx_child_01", ownerContext(writer))).ok,
        ).toBe(false);
      });
    });

    describe("gc works under a cleanup claim", () => {
      /** A live child sandbox row its owner has marked releasing, as deleting the thread does. */
      async function releasing() {
        const f = await parent();
        const fake = harness.make(SCRIPT).sandbox;
        const writer = unwrap(
          await f.store.beginFork({
            parent: ROOT,
            atSeq: 4,
            branch: CHILD,
            holderId: "c",
          }),
        );
        const row = unwrap(
          await f.store.ledger.begin(writer, "sandbox", "fake"),
        );
        const made = unwrap(
          await fake.restore(
            "snap_01",
            manifestHash([]),
            row.operation_key,
            ownerContext(writer),
          ),
        );
        unwrap(
          await f.store.ledger.live(writer, row.resource_id, made.id, null),
        );
        unwrap(await f.store.ledger.releasing(writer, row.resource_id));
        return { f, fake };
      }

      test("after the owning branch is deleted, gc still releases its resource", async () => {
        const { f, fake } = await releasing();
        for (const table of ["events", "leases", "branches"])
          await run(f.db, `DELETE FROM ${table} WHERE branch_id = ?`, [CHILD]);
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
        expect(
          unwrap(await f.store.ledger.rows()).map((r) => r.release_outcome),
        ).toEqual(["released"]);
      });
    });

    // the exact adversarial schedules of the #153 repros, kept as regression tests.
    describe(" repro schedules", () => {
      test("stale creator: takeover and recovery run inside the old creator's restore, before the provider", async () => {
        const f = await parent();
        const { sandbox: base, creates } = harness.make(SCRIPT);
        let recovery: Awaited<ReturnType<typeof recoverFork>> | undefined;
        const sandbox: Sandbox = {
          ...base,
          restore: async (snapshotId, expected, operationKey, context) => {
            f.clock.now += 31_001;
            recovery = await recoverFork(f.store, base, CHILD, "new-owner");
            return base.restore(snapshotId, expected, operationKey, context);
          },
        };
        const point = unwrap(await f.store.read(ROOT)).events.at(-1)?.event
          .event_id;
        if (point === undefined)
          throw new Error("the parent ends with its snapshot");
        const forked = await forkBranch(f.store, sandbox, {
          ...request,
          point,
          holderId: "old-owner",
        });

        expect(code(forked)).toBe("stale_epoch");
        expect(recovery?.ok ? recovery.value : recovery?.error.code).toEqual([
          retired(base),
        ]);
        expect(unwrap(await f.store.branchState(CHILD))).toBe("fork_failed");
        expect(
          unwrap(await f.store.ledger.rows(CHILD)).map((r) => [
            r.state,
            r.release_outcome,
          ]),
        ).toEqual([
          retired(base) === "released"
            ? ["released", "not_created"]
            : ["unknown", null],
        ]);
        expect(creates()).toBe(0);
        expect((await base.attach("sbx_child_01", CTX)).ok).toBe(false);
        expect(unwrap(await collect(f.store.ledger, base))).toEqual([]);
      });

      test("wrong provider: a final not_found from another adapter settles nothing", async () => {
        const f = await parent();
        const creator = unwrap(
          await f.store.beginFork({
            parent: ROOT,
            atSeq: 4,
            branch: CHILD,
            holderId: "creator",
          }),
        );
        const actual = harness.make(SCRIPT).sandbox;
        const row = unwrap(
          await f.store.ledger.begin(creator, "sandbox", "fake"),
        );
        await actual.restore(
          "snap_01",
          manifestHash([]),
          row.operation_key,
          CTX,
        );
        f.clock.now += 31_001;
        const otherBase = harness.make({}).sandbox;
        const wrong: Sandbox = {
          ...otherBase,
          info: { ...otherBase.info, provider: "wrong" },
        };

        const recovered = await recoverFork(f.store, wrong, CHILD, "recovery");
        expect(code(recovered)).toBe("invalid_request");
        expect(
          unwrap(await f.store.ledger.rows(CHILD)).map((r) => [
            r.provider,
            r.state,
            r.release_outcome,
          ]),
        ).toEqual([["fake", "pending", null]]);
        expect((await actual.attach("sbx_child_01", CTX)).ok).toBe(true);
      });
    });
  });
}
