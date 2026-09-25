import { z } from "zod";
import { BranchId, Int } from "../log";
import type { EnumOf, Strict } from "../log/zod-types";
import { err, ok, type Result } from "../result";
import { type LogError, logError } from "../verify/error";
import { READ_ONLY, type StoreDriver, type Tx } from "./driver";
import { uuidv7 } from "./encode";
import { atomically, getLease, ownedBranch, parseRows } from "./tables";
import type { Writer } from "./writer";

// The resource ledger: one row per provider resource, written `pending` with
// its operation key before the provider call that creates it. Rows move only through the named
// transitions below. The owner's current lease holder makes every move (fenced like an append),
// except cleanup, which may only finish releasing rows (or retire expired snapshots) of an owner
// nobody holds. Every statement is scoped to this store's tenant.

const STATES = [
  "pending",
  "live",
  "releasing",
  "released",
  "release_failed",
  "unknown",
] as const;
const KINDS = ["sandbox", "snapshot"] as const;

export const ResourceRow: Strict<{
  resource_id: z.ZodString;
  tenant_id: z.ZodString;
  owner_branch_id: typeof BranchId;
  provider: z.ZodString;
  kind: EnumOf<typeof KINDS>;
  ref: z.ZodNullable<z.ZodString>;
  state: EnumOf<typeof STATES>;
  operation_key: z.ZodString;
  acquired_at: typeof Int;
  expires_at: z.ZodNullable<typeof Int>;
  released_at: z.ZodNullable<typeof Int>;
  release_outcome: z.ZodNullable<z.ZodString>;
  cleanup_claim: z.ZodNullable<z.ZodString>;
}> = z.strictObject({
  resource_id: z.string(),
  tenant_id: z.string(),
  owner_branch_id: BranchId,
  provider: z.string(),
  kind: z.enum(KINDS),
  ref: z.string().nullable(),
  state: z.enum(STATES),
  operation_key: z.string(),
  acquired_at: Int,
  expires_at: Int.nullable(),
  released_at: Int.nullable(),
  release_outcome: z.string().nullable(),
  cleanup_claim: z.string().nullable(),
});
export type ResourceRow = z.infer<typeof ResourceRow>;
export type ResourceState = ResourceRow["state"];

type Change = {
  readonly ref?: string;
  readonly expiresAt?: number | null;
  /** How a release ended: released, already_gone, not_created, or the failure. */
  readonly outcome?: string;
};

const COLUMNS = `resource_id, tenant_id, owner_branch_id, provider, kind, ref, state, operation_key,
  acquired_at, expires_at, released_at, release_outcome, cleanup_claim`;

export class ResourceLedger {
  readonly #db: StoreDriver;
  readonly #now: () => number;
  readonly #tenant: string;

  constructor(db: StoreDriver, now: () => number, tenantId: string) {
    this.#db = db;
    this.#now = now;
    this.#tenant = tenantId;
  }

  /** Writes the `pending` row with a fresh operation key, before the provider call. */
  begin(
    writer: Writer,
    kind: ResourceRow["kind"],
    provider: string,
  ): Promise<Result<ResourceRow, LogError>> {
    return writer.fenced(async (tx) => {
      const branch = await ownedBranch(tx, writer.lease.branchId, this.#tenant);
      if (!branch.ok) return branch;
      const now = this.#now();
      const row: ResourceRow = {
        resource_id: uuidv7(now),
        tenant_id: this.#tenant,
        owner_branch_id: branch.value.branch_id,
        provider,
        kind,
        ref: null,
        state: "pending",
        operation_key: uuidv7(now),
        acquired_at: now,
        expires_at: null,
        released_at: null,
        release_outcome: null,
        cleanup_claim: null,
      };
      await tx.run(
        `INSERT INTO resources (${COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
        [
          row.resource_id,
          row.tenant_id,
          row.owner_branch_id,
          row.provider,
          row.kind,
          row.ref,
          row.state,
          row.operation_key,
          row.acquired_at,
          row.expires_at,
          row.released_at,
          row.release_outcome,
          row.cleanup_claim,
        ],
      );
      return ok(row);
    });
  }

  /** The provider made it (or a lookup found it): pending, or unknown by an operator, → live. */
  live(
    writer: Writer,
    resourceId: string,
    ref: string,
    expiresAt: number | null,
  ): Promise<Result<ResourceRow, LogError>> {
    return this.#owned(writer, resourceId, ["pending", "unknown"], "live", {
      ref,
      expiresAt,
    });
  }

  /** A final not_found: nothing was created. */
  notCreated(
    writer: Writer,
    resourceId: string,
  ): Promise<Result<ResourceRow, LogError>> {
    return this.#owned(writer, resourceId, ["pending"], "released", {
      outcome: "not_created",
    });
  }

  /** The outcome can't be established: parks for an operator (resource_unknown). */
  unknown(
    writer: Writer,
    resourceId: string,
  ): Promise<Result<ResourceRow, LogError>> {
    return this.#owned(writer, resourceId, ["pending", "releasing"], "unknown");
  }

  /** The owner starts releasing a live row, or retries a failed release. */
  releasing(
    writer: Writer,
    resourceId: string,
  ): Promise<Result<ResourceRow, LogError>> {
    return this.#owned(
      writer,
      resourceId,
      ["live", "release_failed"],
      "releasing",
    );
  }

  /** The owner's release call answered. */
  released(
    writer: Writer,
    resourceId: string,
    outcome: string,
  ): Promise<Result<ResourceRow, LogError>> {
    return this.#owned(writer, resourceId, ["releasing"], "released", {
      outcome,
    });
  }

  releaseFailed(
    writer: Writer,
    resourceId: string,
    reason: string,
  ): Promise<Result<ResourceRow, LogError>> {
    return this.#owned(writer, resourceId, ["releasing"], "release_failed", {
      outcome: reason,
    });
  }

  /**
   * What gc may finish: this tenant's rows `releasing` or `release_failed`, and `live` rows
   * whose provider expiry has passed, of owners nobody holds (a deleted owner holds nothing).
   */
  collectable(): Promise<Result<readonly ResourceRow[], LogError>> {
    return this.#db.transaction(async (tx) => {
      const rows = await this.#rows(tx);
      if (!rows.ok) return rows;
      const due: ResourceRow[] = [];
      for (const row of rows.value)
        if (await this.#collectable(tx, row)) due.push(row);
      return ok(due);
    }, READ_ONLY);
  }

  /**
   * gc's claim on a collectable row, by compare-and-set: a new token replaces any older claim,
   * so only the latest claimant's fence passes (spec/api.json SandboxAuthority cleanup).
   */
  claim(resourceId: string): Promise<Result<string, LogError>> {
    return atomically(this.#db, async (tx) => {
      const row = await this.#get(tx, resourceId);
      if (!row.ok) return row;
      if (!(await this.#collectable(tx, row.value)))
        return err(
          logError("invalid_transition", `${resourceId} is not collectable`),
        );
      const token = uuidv7(this.#now());
      await tx.run(
        `UPDATE resources SET cleanup_claim = ? WHERE tenant_id = ? AND resource_id = ?
          AND cleanup_claim IS NOT DISTINCT FROM ?`,
        [token, this.#tenant, resourceId, row.value.cleanup_claim],
      );
      return ok(token);
    });
  }

  /** The cleanup fence: the row still carries `claim` and is still collectable. */
  claimed(
    resourceId: string,
    claim: string,
  ): Promise<Result<ResourceRow, LogError>> {
    return this.#db.transaction(
      (tx) => this.#claimed(tx, resourceId, claim),
      READ_ONLY,
    );
  }

  async #claimed(
    tx: Tx,
    resourceId: string,
    claim: string,
  ): Promise<Result<ResourceRow, LogError>> {
    const row = await this.#get(tx, resourceId);
    if (!row.ok) return row;
    return row.value.cleanup_claim === claim &&
      (await this.#collectable(tx, row.value))
      ? row
      : err(
          logError("stale_epoch", `the cleanup claim on ${resourceId} is gone`),
        );
  }

  /** gc's release outcome, under its current claim. It can't create, resolve or reopen anything. */
  collected(
    resourceId: string,
    claim: string,
    to: "released" | "release_failed",
    outcome: string,
  ): Promise<Result<ResourceRow, LogError>> {
    return atomically(this.#db, async (tx) => {
      const row = await this.#claimed(tx, resourceId, claim);
      return row.ok ? this.#write(tx, row.value, to, { outcome }) : row;
    });
  }

  /** This tenant's rows, oldest first, optionally only one owner's. */
  rows(owner?: string): Promise<Result<readonly ResourceRow[], LogError>> {
    return this.#db.transaction((tx) => this.#rows(tx, owner), READ_ONLY);
  }

  async #rows(
    tx: Tx,
    owner?: string,
  ): Promise<Result<readonly ResourceRow[], LogError>> {
    const where =
      owner === undefined
        ? "tenant_id = ?"
        : "tenant_id = ? AND owner_branch_id = ?";
    const params = owner === undefined ? [this.#tenant] : [this.#tenant, owner];
    return parseRows(
      ResourceRow,
      await tx.all(
        `SELECT ${COLUMNS} FROM resources WHERE ${where} ORDER BY rowid`,
        params,
      ),
    );
  }

  #owned(
    writer: Writer,
    resourceId: string,
    from: readonly ResourceState[],
    to: ResourceState,
    change: Change = {},
  ): Promise<Result<ResourceRow, LogError>> {
    return writer.fenced(async (tx) => {
      const row = await this.#get(tx, resourceId);
      if (!row.ok) return row;
      if (row.value.owner_branch_id !== writer.lease.branchId)
        return err(logError("stale_epoch", "the writer does not own this row"));
      if (!from.includes(row.value.state))
        return err(
          logError(
            "invalid_transition",
            `a ${row.value.state} resource can't become ${to}`,
          ),
        );
      return this.#write(tx, row.value, to, change);
    });
  }

  /** A compare-and-set on the row's state: doing it again after an unknown commit is a no-op. */
  async #write(
    tx: Tx,
    row: ResourceRow,
    to: ResourceState,
    change: Change,
  ): Promise<Result<ResourceRow, LogError>> {
    const done = to === "released" || to === "release_failed";
    const next: ResourceRow = {
      ...row,
      state: to,
      ref: change.ref ?? row.ref,
      expires_at:
        change.expiresAt === undefined ? row.expires_at : change.expiresAt,
      released_at: done ? this.#now() : row.released_at,
      release_outcome: change.outcome ?? row.release_outcome,
    };
    await tx.run(
      `UPDATE resources SET state = ?, ref = ?, expires_at = ?, released_at = ?,
        release_outcome = ? WHERE tenant_id = ? AND resource_id = ? AND state = ?`,
      [
        next.state,
        next.ref,
        next.expires_at,
        next.released_at,
        next.release_outcome,
        this.#tenant,
        row.resource_id,
        row.state,
      ],
    );
    return ok(next);
  }

  async #get(
    tx: Tx,
    resourceId: string,
  ): Promise<Result<ResourceRow, LogError>> {
    const rows = parseRows(
      ResourceRow,
      await tx.all(
        `SELECT ${COLUMNS} FROM resources WHERE tenant_id = ? AND resource_id = ?`,
        [this.#tenant, resourceId],
      ),
    );
    if (!rows.ok) return rows;
    const row = rows.value[0];
    return row === undefined
      ? err(logError("not_found", `no resource ${resourceId}`))
      : ok(row);
  }

  async #collectable(tx: Tx, r: ResourceRow): Promise<boolean> {
    const due =
      r.state === "releasing" ||
      r.state === "release_failed" ||
      (r.state === "live" &&
        r.expires_at !== null &&
        r.expires_at <= this.#now());
    return due && (await this.#unheld(tx, r.owner_branch_id));
  }

  async #unheld(tx: Tx, branchId: string): Promise<boolean> {
    const lease = await getLease(tx, branchId);
    return (
      lease.ok &&
      (lease.value === undefined || lease.value.expires_at <= this.#now())
    );
  }
}
