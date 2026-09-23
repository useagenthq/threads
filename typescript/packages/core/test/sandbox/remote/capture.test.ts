import { describe, expect, test } from "bun:test";
import { captureSnapshot, collect, type Sandbox } from "../../../src/sandbox";
import { resolvePending } from "../../../src/sandbox/ledger";
import { remoteSandbox, type SandboxDriver } from "../../../src/sandbox/remote";
import {
  code,
  fixture,
  ROOT,
  started,
  THREAD,
  unwrap,
} from "../../store/helpers";
import { CTX } from "../context";
import { memoryDriver } from "./memory-driver";
import { World } from "./world";

// A snapshot capture under the resource ledger: the snapshot row, and the scratch sandbox that
// proves the image holds its manifest, each pending before its provider call and
// settled after; a crash between them leaves rows recovery settles.

const INFO = {
  provider: "memory",
  egress: "enforced",
  browser: "none",
  desktop: "none",
} as const;
const EXPIRY = { sandboxMs: 60_000, snapshotMs: null };
const LEASE = 30_000;
const utf8 = new TextEncoder();

class Crash extends Error {}

function owner() {
  const f = fixture();
  unwrap(f.store.createBranch(THREAD, ROOT));
  const writer = unwrap(f.store.acquire(ROOT, "holder-a"));
  unwrap(writer.append([started]));
  return { ...f, writer };
}

async function parent(
  world: World,
  driver: SandboxDriver = memoryDriver(world),
) {
  const sandbox = remoteSandbox(driver, INFO, EXPIRY);
  const box = unwrap(await sandbox.create("op", CTX));
  unwrap(await box.upload("value.txt", utf8.encode("A"), CTX));
  return { sandbox, box };
}

const rows = (f: ReturnType<typeof owner>) =>
  unwrap(f.store.ledger.rows()).map((r) => [r.kind, r.state]);

describe("a ledgered snapshot capture", () => {
  test("the image is verified by a scratch sandbox that is ledgered and released", async () => {
    const world = new World();
    const f = owner();
    const { sandbox, box } = await parent(world);
    const snap = unwrap(
      await captureSnapshot(f.store.ledger, f.writer, sandbox, box),
    ).data;
    expect(snap.manifest_hash).toBe(world.hashOf(snap.snapshot_id));
    expect(rows(f)).toEqual([
      ["snapshot", "live"],
      ["sandbox", "released"],
    ]);
    expect([...world.machines.keys()]).toEqual([box.id]);
  });

  test("A→B just before the capture, B→A right after: refused, not A's hash", async () => {
    const world = new World();
    const base = memoryDriver(world);
    const capture = base.snapshot;
    if (capture === undefined) throw new Error("the memory driver captures");
    const file = "/workspace/value.txt";
    const { sandbox, box } = await parent(world, {
      ...base,
      snapshot: {
        ...capture,
        take: async (id, key) => {
          world.machine(id).write(file, utf8.encode("B"));
          const made = await capture.take(id, key);
          world.machine(id).write(file, utf8.encode("A"));
          return made;
        },
      },
    });
    const f = owner();
    const snap = await captureSnapshot(f.store.ledger, f.writer, sandbox, box);
    expect(code(snap)).toBe("not_quiescent");
    expect(world.snapshots.size).toBe(0);
    // The restore released the scratch sandbox itself on the mismatch (its contract); a
    // nonfinal lookup can't prove that, so the row parks for an operator rather than guess.
    expect(rows(f)).toEqual([
      ["snapshot", "released"],
      ["sandbox", "unknown"],
    ]);
    expect([...world.machines.keys()]).toEqual([box.id]);
  });

  test("a crash between the scratch create and its kill: the live row is released on recovery", async () => {
    const world = new World();
    const f = owner();
    const { sandbox, box } = await parent(world);
    // The host dies as it closes the scratch sandbox: the row is releasing, the sandbox lives.
    const dies: Sandbox = {
      ...sandbox,
      restore: async (...args) => {
        const made = await sandbox.restore(...args);
        if (!made.ok) return made;
        return {
          ok: true,
          value: {
            ...made.value,
            close: async () => {
              throw new Crash("the host died");
            },
          },
        };
      },
    };
    await expect(
      captureSnapshot(f.store.ledger, f.writer, dies, box),
    ).rejects.toThrow(Crash);
    await Bun.sleep(0);
    expect(world.machines.size).toBe(2);
    expect(rows(f)).toEqual([
      ["snapshot", "live"],
      ["sandbox", "releasing"],
    ]);
    f.clock.now += LEASE + 1; // the dead owner's lease lapses; gc settles the row
    const done = unwrap(await collect(f.store.ledger, sandbox));
    expect(done.map((r) => [r.kind, r.state])).toEqual([
      ["sandbox", "released"],
    ]);
    expect([...world.machines.keys()]).toEqual([box.id]);
  });

  test("a crash inside the scratch restore: the pending row is found by its key and released", async () => {
    const world = new World();
    const f = owner();
    const { sandbox, box } = await parent(world);
    const dies: Sandbox = {
      ...sandbox,
      restore: async (...args) => {
        await sandbox.restore(...args);
        throw new Crash("the host died");
      },
    };
    await expect(
      captureSnapshot(f.store.ledger, f.writer, dies, box),
    ).rejects.toThrow(Crash);
    await Bun.sleep(0);
    const pending = unwrap(f.store.ledger.rows()).find(
      (r) => r.kind === "sandbox",
    );
    if (pending === undefined)
      throw new Error("the scratch row was written first");
    expect(pending.state).toBe("pending");
    f.clock.now += LEASE + 1;
    const next = unwrap(f.store.acquire(ROOT, "restarted"));
    expect(
      unwrap(await resolvePending(f.store.ledger, next, sandbox, pending)),
    ).toBe("released");
    expect([...world.machines.keys()]).toEqual([box.id]);
  });
});
