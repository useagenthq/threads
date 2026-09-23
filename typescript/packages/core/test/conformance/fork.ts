import { expect } from "bun:test";
import { z } from "zod";
import { storeOf } from "../../src/agent/sqlite";
import { BranchId, EventId } from "../../src/log";
import { knownEvents } from "../../src/reduce";
import {
  type FakeSandbox,
  fakeSandbox,
  type Sandbox,
  SandboxScript,
} from "../../src/sandbox";
import { LEASE_TTL_MS } from "../../src/store";
import { openThread } from "../../src/thread";
import { forkBranch, recoverForks } from "../../src/thread/fork";
import { verifyExport } from "../../src/verify";
import { count, fixture, unwrap } from "../store/helpers";
import { type Case, caseStore } from "./cases";
import { expectAppended } from "./recover";

// The fork kind (spec/conformance/README.md): fork against the fake sandbox, then check the
// child, the parent, the sandboxes and the resource ledger the operation left.

class Crash extends Error {}

/** The host dies right after the restore returns, before the child's fork event. */
function dies(sandbox: FakeSandbox): Sandbox {
  return {
    ...sandbox,
    restore: async (...args) => {
      await sandbox.restore(...args);
      throw new Crash("the host died");
    },
  };
}

const Input = z.strictObject({
  fork_at_event_id: EventId,
  new_branch_id: BranchId,
  knowledge_policy: z.enum(["pinned", "current"]).optional(),
});

export async function runFork(c: Case, bytes: Uint8Array): Promise<void> {
  const input = Input.parse(c.input);
  const f = caseStore(c);
  f.clock.now = c.now;
  const imported = unwrap(f.store.importLog(bytes));
  const leaf = imported.segments.at(-1)?.header;
  if (leaf === undefined) throw new Error("a verified log has a header");
  const parent = leaf.branch_id;
  const before = unwrap(f.store.exportBranch(parent));
  const script = SandboxScript.parse(c.scripts.sandbox ?? {});
  const sandbox = fakeSandbox(script);
  const crash = Object.values(script.snapshots ?? {}).some(
    (s) => s.restore_response === "crash",
  );

  await operate(c, f, crash ? dies(sandbox) : sandbox, sandbox, parent, input);
  if (c.fork?.child_state !== undefined)
    expect<unknown>(unwrap(f.store.branchState(input.new_branch_id))).toBe(
      c.fork.child_state,
    );
  if (c.fork?.parent_unchanged === true)
    expect(unwrap(f.store.exportBranch(parent))).toEqual(before);

  const handle = await openThread(
    storeOf({ log: f.store, artifacts: f.artifacts }),
    leaf.thread_id,
    { branchId: input.new_branch_id },
  );
  expect(handle.ok).toBe(c.fork?.child_created ?? false);
  if (handle.ok) checkChild(c, f, input.new_branch_id);
  else await checkNothingLeft(c, f, sandbox, script);
  if (c.resources !== undefined) {
    expect(sandbox.creates()).toBe(c.resources.creates);
    expect<unknown>(
      unwrap(f.store.ledger.rows()).map((r) => ({
        kind: r.kind,
        state: r.state,
      })),
    ).toEqual(c.resources.rows);
  }
  f.db.close();
}

/** A failed fork appends nothing and leaves no sandbox, except a create it can't establish. */
async function checkNothingLeft(
  c: Case,
  f: ReturnType<typeof caseStore>,
  sandbox: FakeSandbox,
  script: SandboxScript,
): Promise<void> {
  expect(c.appended ?? []).toEqual([]);
  const rows = unwrap(f.store.ledger.rows());
  for (const row of rows) expect(["released", "unknown"]).toContain(row.state);
  if (rows.some((r) => r.state === "unknown")) return;
  for (const snap of Object.values(script.snapshots ?? {})) {
    const attached = await sandbox.attach(snap.restore_sandbox_id);
    expect(attached.ok).toBe(false);
  }
}

/** The fork, or for restore_response crash: the fork dies, the host restarts, recovery runs. */
async function operate(
  c: Case,
  f: ReturnType<typeof caseStore>,
  used: Sandbox,
  sandbox: FakeSandbox,
  parent: BranchId,
  input: z.infer<typeof Input>,
): Promise<void> {
  const forking = forkBranch(f.store, used, {
    parent,
    point: input.fork_at_event_id,
    child: input.new_branch_id,
    knowledge: input.knowledge_policy ?? "pinned",
    holderId: "conformance-runner",
  });
  if (used !== sandbox) {
    expect(forking).rejects.toThrow(Crash);
    await forking.catch(() => undefined);
    f.clock.now += LEASE_TTL_MS + 1; // the dead process's lease lapses
    const failed = unwrap(await recoverForks(f.store, sandbox, "restarted"));
    expect(failed).toEqual([input.new_branch_id]);
    return;
  }
  const forked = await forking;
  expect<unknown>(
    forked.ok ? undefined : { code: forked.error.code, seq: forked.error.seq },
  ).toEqual(
    c.error === undefined
      ? undefined
      : { code: c.error.code, seq: c.error.seq },
  );
}

function checkChild(
  c: Case,
  f: ReturnType<typeof caseStore>,
  child: BranchId,
): void {
  const log = unwrap(f.store.read(child));
  const own = log.segments.at(-1);
  expect(own?.header.branch_id).toBe(child);
  const appended = knownEvents(log).filter((e) => e.branch_id === child);
  expectAppended(appended, c.appended ?? []);
  expect(count(f.db, child)).toEqual({ n: appended.length }); // no parent row copied
  const fork = appended[0];
  if (fork?.type !== "fork")
    throw new Error("the child's first event is its fork");
  expect(fork.seq - 1).toBe(c.fork?.at_seq ?? -1);
  if (c.fork?.knowledge_revision !== undefined) {
    const snapshot = knownEvents(log).find((e) => e.seq === fork.seq - 1);
    const pinned =
      snapshot?.type === "snapshot"
        ? (snapshot.data.knowledge_revision ?? null)
        : null;
    expect(fork.data.knowledge_policy === "pinned" ? pinned : null).toBe(
      c.fork.knowledge_revision,
    );
  }
  // The child's export imports cleanly on its own.
  const exported = unwrap(f.store.exportBranch(child));
  expect(verifyExport(exported).ok).toBe(true);
  const other = fixture();
  for (const a of c.artifacts) other.artifacts.put(a);
  expect(unwrap(other.store.importLog(exported)).segments.length).toBe(2);
  other.db.close();
}
