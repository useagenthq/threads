import { err, type Result } from "../result";
import { isRefusal, ownerContext } from "../sandbox/context";
import { resolvePending } from "../sandbox/ledger";
import type { Sandbox, SandboxSession } from "../sandbox/protocol";
import type { ResourceLedger } from "../store/ledger";
import type { Writer } from "../store/writer";
import type { SessionFailure } from "./builtin";

// The run's sandbox session for built-in tools, made on first use through the resource ledger
// like a fork's child: the pending row and its operation key are durable
// before the create call. A branch that already has a live sandbox reattaches to it, so a
// thread keeps its files across runs.

type Got = Result<SandboxSession, SessionFailure>;

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

async function create(
  ledger: ResourceLedger,
  writer: Writer,
  sandbox: Sandbox,
): Promise<Got> {
  const row = await ledger.begin(writer, "sandbox", sandbox.info.provider);
  if (!row.ok) return lost(row.error.message);
  const made = await sandbox.create(
    row.value.operation_key,
    ownerContext(writer),
  );
  if (made.ok) {
    const live = await ledger.live(
      writer,
      row.value.resource_id,
      made.value.id,
      null,
    );
    return live.ok ? made : lost(live.error.message);
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
): () => Promise<Got> {
  let made: Promise<Got> | undefined;
  return async () => {
    made ??= (async () =>
      (await reattach(ledger, writer, sandbox)) ??
      create(ledger, writer, sandbox))();
    const got = await made;
    if (!got.ok) made = undefined;
    return got;
  };
}
