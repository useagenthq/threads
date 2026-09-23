import { err, ok, type Result } from "../result";
import type { ResourceLedger, ResourceRow } from "../store/ledger";
import type { Writer } from "../store/writer";
import { type LogError, logError } from "../verify/error";
import { isRefusal, ownerContext } from "./context";
import { release, resolvePending } from "./ledger";
import type {
  Failure,
  Sandbox,
  SandboxSession,
  SnapshotData,
} from "./protocol";

// A snapshot capture under the resource ledger. The
// snapshot row is pending before the capture. Then the image is proven to hold the tree its
// manifest names by restoring it into a scratch sandbox, which is a provider
// resource like any other: its own pending row before the restore, live after, released after
// close. A crash in between leaves rows that recovery settles by lookup, or parks as unknown.

type CaptureFailure = Failure<"not_quiescent" | "unavailable" | "timeout">;

export type Captured = Result<SnapshotData, LogError | CaptureFailure>;

const refused = (message: string): LogError => logError("stale_epoch", message);

/** Settles the scratch row a restore left: a found sandbox is released, else it parks. */
async function settleScratch(
  ledger: ResourceLedger,
  writer: Writer,
  sandbox: Sandbox,
  row: ResourceRow,
): Promise<Result<void, LogError>> {
  const settled = await resolvePending(ledger, writer, sandbox, row);
  return settled.ok ? ok(undefined) : settled;
}

/**
 * Verifies the image by a ledgered restore against the declared manifest hash. Ok: the scratch
 * sandbox is released. A mismatch or failed restore: the image isn't the tree it names.
 */
async function verifyImage(
  ledger: ResourceLedger,
  writer: Writer,
  sandbox: Sandbox,
  data: SnapshotData,
): Promise<Result<boolean, LogError>> {
  const row = ledger.begin(writer, "sandbox", sandbox.info.provider);
  if (!row.ok) return row;
  const scratch = await sandbox.restore(
    data.snapshot_id,
    data.manifest_hash,
    row.value.operation_key,
    ownerContext(writer),
  );
  if (!scratch.ok) {
    if (isRefusal(scratch.error)) return err(refused(scratch.error.message));
    const settled = await settleScratch(ledger, writer, sandbox, row.value);
    return settled.ok ? ok(false) : settled;
  }
  const live = ledger.live(
    writer,
    row.value.resource_id,
    scratch.value.id,
    null,
  );
  if (!live.ok) return live;
  const released = await release(
    ledger,
    writer,
    sandbox,
    live.value,
    scratch.value,
  );
  return released.ok ? ok(true) : released;
}

/**
 * Captures `session` into a snapshot whose image is verified. A capture whose image doesn't hold
 * its manifest is released and refused (not_quiescent).
 */
export async function captureSnapshot(
  ledger: ResourceLedger,
  writer: Writer,
  sandbox: Sandbox,
  session: SandboxSession,
): Promise<Captured> {
  const row = ledger.begin(writer, "snapshot", sandbox.info.provider);
  if (!row.ok) return row;
  const taken = await session.snapshot(
    row.value.operation_key,
    ownerContext(writer),
  );
  if (!taken.ok) {
    if (isRefusal(taken.error)) return err(refused(taken.error.message));
    // The adapter reports what it did; without a snapshot lookup the row parks for an operator.
    const parked = ledger.unknown(writer, row.value.resource_id);
    return parked.ok ? err(taken.error) : parked;
  }
  const data = taken.value;
  const live = ledger.live(
    writer,
    row.value.resource_id,
    data.snapshot_id,
    data.expires_at,
  );
  if (!live.ok) return live;
  const verified = await verifyImage(ledger, writer, sandbox, data);
  if (!verified.ok) return verified;
  if (verified.value) return ok(data);
  const dropped = await release(ledger, writer, sandbox, live.value);
  if (!dropped.ok) return dropped;
  return err({
    code: "not_quiescent",
    message: `the image of ${data.snapshot_id} doesn't hold manifest ${data.manifest_hash}`,
  });
}
