import type { BranchId, EventId } from "../log";
import { knownEvents } from "../reduce";
import { err, ok, type Result } from "../result";
import {
  lookupSandbox,
  release,
  resolvePending,
  settle,
} from "../sandbox/ledger";
import type {
  RestoreFailure,
  Sandbox,
  SandboxSession,
} from "../sandbox/protocol";
import type { LogStore, ResourceRow, ResourceState, Writer } from "../store";
import { type LogError, logError } from "../verify/error";

// fork(): a new branch restored into an isolated sandbox, and only then
// visible. The child's ledger row and its operation key are durable before the restore call; a
// lost response resolves by that key, never by creating again.

export type KnowledgePolicy = "pinned" | "current";

export type ForkInput = {
  readonly parent: BranchId;
  readonly point: EventId;
  readonly child: BranchId;
  readonly knowledge: KnowledgePolicy;
  readonly holderId: string;
};

/** The snapshot event `point` names on the parent's chain, or no_snapshot_boundary. */
function snapshotAt(log: LogStore, input: ForkInput) {
  const parent = log.read(input.parent);
  if (!parent.ok) return parent;
  const event = knownEvents(parent.value).find(
    (e) => e.event_id === input.point,
  );
  if (event === undefined)
    return err(
      logError(
        "no_snapshot_boundary",
        `no event ${input.point} on this branch`,
      ),
    );
  return ok({
    seq: event.seq,
    snapshot: event.type === "snapshot" ? event.data : undefined,
  });
}

/** Creates the child branch, restores its sandbox, and makes it ready; else fails it cleanly. */
export async function forkBranch(
  log: LogStore,
  sandbox: Sandbox | undefined,
  input: ForkInput,
): Promise<Result<void, LogError>> {
  const at = snapshotAt(log, input);
  if (!at.ok) return at;
  const writer = log.beginFork({
    parent: input.parent,
    atSeq: at.value.seq,
    branch: input.child,
    holderId: input.holderId,
  });
  if (!writer.ok) return writer;
  const snapshot = at.value.snapshot;
  if (snapshot === undefined)
    throw new Error("an eligible point is a snapshot");
  const fail = (code: LogError["code"], message: string) => {
    const failed = log.failFork(writer.value);
    return failed.ok ? err(logError(code, message, at.value.seq)) : failed;
  };
  if (sandbox?.info.provider !== snapshot.provider)
    return fail(
      "snapshot_restore_failed",
      `no ${snapshot.provider} sandbox adapter to restore with`,
    );
  const restored = await restore(
    log,
    writer.value,
    sandbox,
    snapshot.snapshot_id,
  );
  if (!restored.ok)
    return restored.error.code === "stale_epoch" ||
      restored.error.code === "writer_poisoned"
      ? restored
      : fail(restored.error.code, restored.error.message);
  // If this fails the lease was lost before the child became visible; the fork's next owner
  // reclaims it (LogStore.reclaimFork) and finds the live row in the ledger.
  return log.finishFork(writer.value, {
    sandboxId: restored.value.id,
    knowledgePolicy: input.knowledge,
  });
}

/**
 * The restore under a ledger row. A lost response (`unavailable`) resolves by lookup: a found
 * sandbox is used, a final not_found fails the fork, anything else leaves the row unknown for an
 * operator (`resource_unknown`). A typed failure releases whatever a lookup still finds.
 */
async function restore(
  log: LogStore,
  writer: Writer,
  sandbox: Sandbox,
  snapshotId: string,
): Promise<Result<SandboxSession, LogError>> {
  const ledger = log.ledger;
  const row = ledger.begin(writer, "sandbox", sandbox.info.provider);
  if (!row.ok) return row;
  const fence = writer.fence();
  if (!fence.ok) return fence;
  const made = await sandbox.restore(snapshotId, row.value.operation_key);
  if (made.ok) {
    const live = ledger.live(
      writer,
      row.value.resource_id,
      made.value.id,
      null,
    );
    return live.ok ? ok(made.value) : live;
  }
  return recover(log, writer, sandbox, row.value, made.error);
}

async function recover(
  log: LogStore,
  writer: Writer,
  sandbox: Sandbox,
  row: ResourceRow,
  failure: RestoreFailure,
): Promise<Result<SandboxSession, LogError>> {
  const ledger = log.ledger;
  const settled = settle(
    ledger,
    writer,
    row,
    await lookupSandbox(sandbox, row.operation_key),
    sandbox.info.lookup.create === "final",
    (s) => s.id,
  );
  if (!settled.ok) return settled;
  const { state } = settled.value;
  if (state === "unknown")
    return err(
      logError(
        "resource_unknown",
        `the child sandbox of operation ${row.operation_key} can't be established`,
      ),
    );
  if (settled.value.state === "live" && failure.code === "unavailable")
    return ok(settled.value.value);
  if (settled.value.state === "live") {
    const released = await release(
      ledger,
      writer,
      sandbox,
      row.resource_id,
      settled.value.value,
    );
    if (!released.ok) return released;
  }
  const code =
    failure.code === "unavailable" ? "snapshot_restore_failed" : failure.code;
  return err(logError(code, failure.message));
}

/**
 * After a crash mid-fork the fork's creator owns cleanup: it retakes the
 * forking branch, settles each ledger row the fork recorded (a pending one by lookup, never by
 * creating again; a live or failed one by release), and marks the branch fork_failed.
 */
export async function recoverFork(
  log: LogStore,
  sandbox: Sandbox,
  branch: BranchId,
  holderId: string,
): Promise<Result<readonly ResourceState[], LogError>> {
  const writer = log.reclaimFork(branch, holderId);
  if (!writer.ok) return writer;
  const rows = log.ledger.rows(branch);
  if (!rows.ok) return rows;
  const states: ResourceState[] = [];
  for (const row of rows.value) {
    const settled =
      row.state === "pending"
        ? await resolvePending(log.ledger, writer.value, sandbox, row)
        : row.state === "live" || row.state === "release_failed"
          ? await release(log.ledger, writer.value, sandbox, row.resource_id)
          : ok(row.state);
    if (!settled.ok) return settled;
    states.push(settled.value);
  }
  const failed = log.failFork(writer.value);
  return failed.ok ? ok(states) : failed;
}
