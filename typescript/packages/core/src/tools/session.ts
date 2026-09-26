import { err, type Result } from "../result";
import { isRefusal, ownerContext } from "../sandbox/context";
import { release, resolvePending } from "../sandbox/ledger";
import type { Sandbox, SandboxSession, Trees } from "../sandbox/protocol";
import type { Tree } from "../sandbox/tree/tree";
import { placeTree } from "../sandbox/trees";
import type { ArtifactStore } from "../store/artifacts";
import type { ResourceLedger } from "../store/ledger";
import type { Writer } from "../store/writer";
import type { SessionFailure } from "./builtin";

// The run's sandbox session for built-in tools, made on first use through the resource ledger
// like a fork's child: the pending row and its operation key are durable
// before the create call. A branch that already has a live sandbox reattaches to it, so a
// thread keeps its files across runs. A thread with workspace inputs (lane 16 E) places the
// pinned tree into the new sandbox before the row goes live, so no tool can reach a sandbox
// whose /workspace isn't the pinned one.

type Got = Result<SandboxSession, SessionFailure>;

/** The pinned workspace tree and the store its file artifacts are in. */
export type Placement = {
  readonly tree: Tree;
  readonly artifacts: Pick<ArtifactStore, "get">;
};

const lost = (message: string): Got => err({ code: "stale_epoch", message });

async function reattach(
  ledger: ResourceLedger,
  writer: Writer,
  sandbox: Sandbox,
): Promise<Got | undefined> {
  const rows = await ledger.rows(writer.lease.branchId);
  if (!rows.ok) return lost(rows.error.message);
  const row = rows.value.findLast(
    (r) =>
      r.kind === "sandbox" &&
      r.state === "live" &&
      r.provider === sandbox.info.provider &&
      r.ref !== null,
  );
  if (row?.ref == null) return undefined;
  const attached = await sandbox.attach(row.ref, ownerContext(writer));
  if (attached.ok) return attached;
  if (isRefusal(attached.error)) return err(attached.error);
  return err({ code: "unavailable", message: attached.error.message });
}

/**
 * Settles this branch's pending sandbox rows before a new create. A crash between the create
 * and `live` (placing a workspace happens in that window) leaves one behind, and `reattach`
 * reads only `live` rows while gc skips `pending`. Capture-scratch and fork rows open their
 * sandboxes through the same `begin`, so this settles those too: safe, because the lease holder
 * doing the settling is the only one who could be using them. Best effort: `resolvePending`
 * releases what it finds and parks what it can't prove, and neither blocks the new create.
 */
async function settlePending(
  ledger: ResourceLedger,
  writer: Writer,
  sandbox: Sandbox,
): Promise<void> {
  const rows = await ledger.rows(writer.lease.branchId);
  if (!rows.ok) return;
  for (const row of rows.value)
    if (
      row.kind === "sandbox" &&
      row.state === "pending" &&
      row.provider === sandbox.info.provider
    )
      await resolvePending(ledger, writer, sandbox, row);
}

/** Places the pinned tree in a fresh sandbox; the failure the tool call gets, or nothing. */
async function place(
  session: SandboxSession,
  writer: Writer,
  placement: Placement,
): Promise<SessionFailure | undefined> {
  const { exportTree, importTree } = session;
  if (exportTree === undefined || importTree === undefined)
    return {
      code: "capability_missing",
      message:
        "workspace: this sandbox's sessions can't import a tree into /workspace",
    };
  const trees: Trees = { exportTree, importTree };
  const placed = await placeTree(
    trees,
    placement.tree,
    placement.artifacts,
    ownerContext(writer),
  );
  if (placed.ok) return undefined;
  const { code, message } = placed.error;
  if (code === "stale_epoch") return { code, message };
  return {
    code: code === "unavailable" ? "unavailable" : "workspace_mismatch",
    message,
  };
}

async function create(
  ledger: ResourceLedger,
  writer: Writer,
  sandbox: Sandbox,
  placement: Placement | undefined,
): Promise<Got> {
  await settlePending(ledger, writer, sandbox);
  const row = await ledger.begin(writer, "sandbox", sandbox.info.provider);
  if (!row.ok) return lost(row.error.message);
  const made = await sandbox.create(
    row.value.operation_key,
    ownerContext(writer),
  );
  if (made.ok) {
    const bad =
      placement === undefined
        ? undefined
        : await place(made.value, writer, placement);
    // The row is live either way: the sandbox exists, and releasing one starts from live.
    const live = await ledger.live(
      writer,
      row.value.resource_id,
      made.value.id,
      null,
    );
    if (!live.ok) return lost(live.error.message);
    if (bad === undefined) return made;
    await release(ledger, writer, sandbox, row.value, made.value);
    return err(bad);
  }
  if (isRefusal(made.error)) return err(made.error);
  // Settle the row by lookup (releasing anything found); creation is never retried blindly.
  await resolvePending(ledger, writer, sandbox, row.value);
  return err(made.error);
}

/** One session per run, made or reattached on first use; a failure is retried on next use. */
export function lazySession(
  ledger: ResourceLedger,
  writer: Writer,
  sandbox: Sandbox,
  placement?: Placement,
): () => Promise<Got> {
  let made: Promise<Got> | undefined;
  return async () => {
    made ??= (async () =>
      (await reattach(ledger, writer, sandbox)) ??
      create(ledger, writer, sandbox, placement))();
    const got = await made;
    if (!got.ok) made = undefined;
    return got;
  };
}
