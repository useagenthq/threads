import { assertNever } from "../assert-never";
import type { LookupResult } from "../model/protocol";
import { err, ok, type Result } from "../result";
import type { ResourceLedger, ResourceRow, ResourceState } from "../store";
import type { Writer } from "../store/writer";
import { type LogError, logError } from "../verify/error";
import { cleanupContext, ownerContext } from "./context";
import type {
  Sandbox,
  SandboxContext,
  SandboxSession,
  SnapshotData,
  Stale,
} from "./protocol";

// The provider side of the resource ledger (spec/api.json LookupResult): how a
// pending row settles by lookup, how the owner releases a row, and how gc finishes releases.
// A row is resolved only with the adapter of its own provider; any other can prove nothing.

export type Settled<T> =
  | { readonly state: "live"; readonly value: T }
  | { readonly state: "released" | "unknown" };

/** Refuses an adapter that isn't the row's provider, before any lookup or release. */
export function sameProvider(
  sandbox: Sandbox,
  row: ResourceRow,
): Result<void, LogError> {
  return row.provider === sandbox.info.provider
    ? ok(undefined)
    : err(
        logError(
          "invalid_request",
          `row ${row.resource_id} belongs to ${row.provider}, not ${sandbox.info.provider}`,
        ),
      );
}

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

/** A refused dispatch as a log failure: the caller lost its authority and must stop. */
function refused(stale: Stale): LogError {
  return logError("stale_epoch", stale.message);
}

/** Asks the adapter about a sandbox create whose response never came. */
export async function lookupSandbox(
  sandbox: Sandbox,
  operationKey: string,
  context: SandboxContext,
): Promise<Result<LookupResult<SandboxSession>, LogError>> {
  if (sandbox.lookup === undefined || sandbox.info.lookup.create === "none")
    return ok({
      status: "unknown",
      reason: "the adapter can't look up a create",
    });
  const answer = await sandbox.lookup(operationKey, context);
  return answer.ok ? answer : err(refused(answer.error));
}

async function lookupSnapshot(
  sandbox: Sandbox,
  operationKey: string,
  context: SandboxContext,
): Promise<Result<LookupResult<SnapshotData>, LogError>> {
  if (
    sandbox.lookupSnapshot === undefined ||
    sandbox.info.lookup.snapshot === "none"
  )
    return ok({
      status: "unknown",
      reason: "the adapter can't look up a snapshot",
    });
  const answer = await sandbox.lookupSnapshot(operationKey, context);
  return answer.ok ? answer : err(refused(answer.error));
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
  const same = sameProvider(sandbox, row);
  if (!same.ok) return same;
  const key = row.operation_key;
  const context = ownerContext(writer);
  if (row.kind === "sandbox") {
    const answer = await lookupSandbox(sandbox, key, context);
    if (!answer.ok) return answer;
    const settled = settle(
      ledger,
      writer,
      row,
      answer.value,
      sandbox.info.lookup.create === "final",
      (s) => s.id,
    );
    if (!settled.ok || settled.value.state !== "live")
      return settled.ok ? ok(settled.value.state) : settled;
    return release(ledger, writer, sandbox, row, settled.value.value);
  }
  const answer = await lookupSnapshot(sandbox, key, context);
  if (!answer.ok) return answer;
  const settled = settle(
    ledger,
    writer,
    row,
    answer.value,
    sandbox.info.lookup.snapshot === "final",
    (s) => s.snapshot_id,
  );
  if (!settled.ok || settled.value.state !== "live")
    return settled.ok ? ok(settled.value.state) : settled;
  return release(ledger, writer, sandbox, row);
}

/** The provider's answer to releasing one resource, as the row's next state and outcome. */
type Released = {
  readonly to: "released" | "release_failed" | "unknown";
  readonly outcome: string;
};

async function provideRelease(
  sandbox: Sandbox,
  row: ResourceRow,
  context: SandboxContext,
  session: SandboxSession | undefined,
): Promise<Released> {
  const ref = row.ref ?? "";
  if (row.kind === "snapshot") {
    const done = await sandbox.release(ref, context);
    return done.ok
      ? { to: "released", outcome: done.value }
      : { to: "release_failed", outcome: done.error.message };
  }
  const attached =
    session === undefined ? await sandbox.attach(ref, context) : undefined;
  if (attached !== undefined && !attached.ok) {
    const gone =
      attached.error.code === "not_found" &&
      sandbox.info.lookup.create === "final";
    if (gone) return { to: "released", outcome: "already_gone" };
    return attached.error.code === "resource_unknown"
      ? { to: "unknown", outcome: attached.error.message }
      : { to: "release_failed", outcome: attached.error.message };
  }
  const target = session ?? attached?.value;
  if (target === undefined) throw new Error("a sandbox row has a session");
  const closed = await target.close(context);
  return closed.ok
    ? { to: "released", outcome: "released" }
    : { to: "release_failed", outcome: closed.error.message };
}

/**
 * The owner releases a live or failed row: `releasing` first, then the provider call under the
 * owner's context, then its outcome. An error is `release_failed`, retried later, never dropped.
 */
export async function release(
  ledger: ResourceLedger,
  writer: Writer,
  sandbox: Sandbox,
  row: ResourceRow,
  session?: SandboxSession,
): Promise<Result<ResourceState, LogError>> {
  const same = sameProvider(sandbox, row);
  if (!same.ok) return same;
  const releasing = ledger.releasing(writer, row.resource_id);
  if (!releasing.ok) return releasing;
  const answer = await provideRelease(
    sandbox,
    releasing.value,
    ownerContext(writer),
    session,
  );
  const id = row.resource_id;
  const done =
    answer.to === "released"
      ? ledger.released(writer, id, answer.outcome)
      : answer.to === "release_failed"
        ? ledger.releaseFailed(writer, id, answer.outcome)
        : ledger.unknown(writer, id);
  return done.ok ? ok(done.value.state) : done;
}

/**
 * `threads gc` for one provider: claims each collectable row of this tenant, releases it under
 * that claim, and records the outcome. It keeps working after the owning branch is deleted. A
 * claim another run took later fences this one off; a failure stays release_failed.
 */
export async function collect(
  ledger: ResourceLedger,
  sandbox: Sandbox,
): Promise<Result<readonly ResourceRow[], LogError>> {
  const rows = ledger.collectable();
  if (!rows.ok) return rows;
  const done: ResourceRow[] = [];
  for (const row of rows.value) {
    if (!sameProvider(sandbox, row).ok) continue;
    const claim = ledger.claim(row.resource_id);
    if (!claim.ok) continue;
    const context = cleanupContext(ledger, row.resource_id, claim.value);
    const answer = await provideRelease(sandbox, row, context, undefined);
    const to = answer.to === "released" ? "released" : "release_failed";
    const moved = ledger.collected(
      row.resource_id,
      claim.value,
      to,
      answer.outcome,
    );
    if (moved.ok) done.push(moved.value);
  }
  return ok(done);
}
