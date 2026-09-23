import { assertNever } from "../assert-never";
import type { LookupResult } from "../model/protocol";
import { ok, type Result } from "../result";
import type { ResourceLedger, ResourceRow, ResourceState } from "../store";
import type { Writer } from "../store/writer";
import type { LogError } from "../verify/error";
import type { Sandbox, SandboxSession, SnapshotData } from "./protocol";

// The provider side of the resource ledger: how a
// pending row settles by lookup, how the owner releases a row, and how gc finishes releases.

export type Settled<T> =
  | { readonly state: "live"; readonly value: T }
  | { readonly state: "released" | "unknown" };

/**
 * Settles a pending row by the adapter's lookup answer: found → live; not_found → released,
 * only when the operation's declared capability is final; anything else → unknown (parks).
 */
export function settle<T>(
  ledger: ResourceLedger,
  writer: Writer,
  row: ResourceRow,
  answer: LookupResult<T>,
  final: boolean,
  refOf: (value: T) => string,
): Result<Settled<T>, LogError> {
  const id = row.resource_id;
  switch (answer.status) {
    case "found": {
      const live = ledger.live(writer, id, refOf(answer.value), null);
      return live.ok ? ok({ state: "live", value: answer.value }) : live;
    }
    case "not_found":
      if (final) {
        const gone = ledger.notCreated(writer, id);
        return gone.ok ? ok({ state: "released" }) : gone;
      }
      return unknown(ledger, writer, id);
    case "not_found_nonfinal":
    case "unknown":
      return unknown(ledger, writer, id);
    default:
      return assertNever(answer);
  }
}

function unknown<T>(
  ledger: ResourceLedger,
  writer: Writer,
  id: string,
): Result<Settled<T>, LogError> {
  const parked = ledger.unknown(writer, id);
  return parked.ok ? ok({ state: "unknown" }) : parked;
}

/** Asks the adapter about a sandbox create whose response never came. */
export async function lookupSandbox(
  sandbox: Sandbox,
  operationKey: string,
): Promise<LookupResult<SandboxSession>> {
  return sandbox.lookup === undefined || sandbox.info.lookup.create === "none"
    ? { status: "unknown", reason: "the adapter can't look up a create" }
    : sandbox.lookup(operationKey);
}

async function lookupSnapshot(
  sandbox: Sandbox,
  operationKey: string,
): Promise<LookupResult<SnapshotData>> {
  return sandbox.lookupSnapshot === undefined ||
    sandbox.info.lookup.snapshot === "none"
    ? { status: "unknown", reason: "the adapter can't look up a snapshot" }
    : sandbox.lookupSnapshot(operationKey);
}

/**
 * The owner settles a pending row left by a crash and releases what it finds. Creation is
 * never retried: the row ends released, or unknown for an operator.
 */
export async function resolvePending(
  ledger: ResourceLedger,
  writer: Writer,
  sandbox: Sandbox,
  row: ResourceRow,
): Promise<Result<ResourceState, LogError>> {
  const key = row.operation_key;
  if (row.kind === "sandbox") {
    const settled = settle(
      ledger,
      writer,
      row,
      await lookupSandbox(sandbox, key),
      sandbox.info.lookup.create === "final",
      (s) => s.id,
    );
    if (!settled.ok || settled.value.state !== "live")
      return settled.ok ? ok(settled.value.state) : settled;
    return release(
      ledger,
      writer,
      sandbox,
      row.resource_id,
      settled.value.value,
    );
  }
  const settled = settle(
    ledger,
    writer,
    row,
    await lookupSnapshot(sandbox, key),
    sandbox.info.lookup.snapshot === "final",
    (s) => s.snapshot_id,
  );
  if (!settled.ok || settled.value.state !== "live")
    return settled.ok ? ok(settled.value.state) : settled;
  return release(ledger, writer, sandbox, row.resource_id);
}

/** The provider's answer to releasing one resource, as the row's next state and outcome. */
type Released = {
  readonly to: "released" | "release_failed" | "unknown";
  readonly outcome: string;
};

async function provideRelease(
  sandbox: Sandbox,
  row: ResourceRow,
  session: SandboxSession | undefined,
): Promise<Released> {
  const ref = row.ref ?? "";
  if (row.kind === "snapshot") {
    const done = await sandbox.release(ref);
    return done.ok
      ? { to: "released", outcome: done.value }
      : { to: "release_failed", outcome: done.error.message };
  }
  const attached =
    session === undefined ? await sandbox.attach(ref) : undefined;
  if (attached !== undefined && !attached.ok) {
    const gone =
      attached.error.code === "not_found" &&
      sandbox.info.lookup.create === "final";
    if (gone) return { to: "released", outcome: "already_gone" };
    return attached.error.code === "unavailable"
      ? { to: "release_failed", outcome: attached.error.message }
      : { to: "unknown", outcome: attached.error.message };
  }
  const target = session ?? attached?.value;
  if (target === undefined) throw new Error("a sandbox row has a session");
  const closed = await target.close();
  return closed.ok
    ? { to: "released", outcome: "released" }
    : { to: "release_failed", outcome: closed.error.message };
}

/**
 * The owner releases a live or failed row: `releasing` first, then the provider call, then its
 * outcome. An error is `release_failed`, retried later, never dropped.
 */
export async function release(
  ledger: ResourceLedger,
  writer: Writer,
  sandbox: Sandbox,
  resourceId: string,
  session?: SandboxSession,
): Promise<Result<ResourceState, LogError>> {
  const row = ledger.releasing(writer, resourceId);
  if (!row.ok) return row;
  const answer = await provideRelease(sandbox, row.value, session);
  const done =
    answer.to === "released"
      ? ledger.released(writer, resourceId, answer.outcome)
      : answer.to === "release_failed"
        ? ledger.releaseFailed(writer, resourceId, answer.outcome)
        : ledger.unknown(writer, resourceId);
  return done.ok ? ok(done.value.state) : done;
}

/**
 * `threads gc` for one provider: finishes this tenant's collectable rows. It only releases;
 * a failure stays release_failed for the next run.
 */
export async function collect(
  ledger: ResourceLedger,
  sandbox: Sandbox,
): Promise<Result<readonly ResourceRow[], LogError>> {
  const rows = ledger.collectable();
  if (!rows.ok) return rows;
  const done: ResourceRow[] = [];
  for (const row of rows.value) {
    if (row.provider !== sandbox.info.provider) continue;
    const answer = await provideRelease(sandbox, row, undefined);
    const to = answer.to === "released" ? "released" : "release_failed";
    const moved = ledger.collected(row.resource_id, to, answer.outcome);
    if (!moved.ok) return moved;
    done.push(moved.value);
  }
  return ok(done);
}
