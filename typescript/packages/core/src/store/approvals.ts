import { z } from "zod";
import { Int, type KnownEvent, principalKey } from "../log";
import type { Strict } from "../log/zod-types";
import { err, ok, type Result } from "../result";
import type { ChainEvent } from "../verify";
import { type LogError, logError } from "../verify/error";
import type { Tx } from "./driver";
import type { BranchRow } from "./tables";
import { parseRows } from "./tables";

// The approvals table (store.sql): one single-use row per approval_requested,
// written in that append's transaction, and consumed by the append of its answer. The log's own
// rule 9 makes a challenge single-use along one chain; the row makes it single-use across every
// branch that inherited it, and binds it to the branch it was asked on.

const STATES = ["open", "granted", "denied", "expired"] as const;

const ApprovalRow: Strict<{
  branch_id: z.ZodString;
  call_id: z.ZodString;
  args_hash: z.ZodString;
  expires_at: typeof Int;
  state: z.ZodEnum<{ [K in (typeof STATES)[number]]: K }>;
}> = z.strictObject({
  branch_id: z.string(),
  call_id: z.string(),
  args_hash: z.string(),
  expires_at: Int,
  state: z.enum(STATES),
});
export type ApprovalRow = z.infer<typeof ApprovalRow>;

export async function approvalRow(
  tx: Tx,
  challengeId: string,
): Promise<Result<ApprovalRow | undefined, LogError>> {
  const rows = parseRows(
    ApprovalRow,
    await tx.all(
      "SELECT branch_id, call_id, args_hash, expires_at, state FROM approvals WHERE challenge_id = ?",
      [challengeId],
    ),
  );
  return rows.ok ? ok(rows.value[0]) : rows;
}

type Answer = Extract<
  KnownEvent,
  { type: "approval_granted" | "approval_denied" }
>;

/**
 * Runs inside an append's transaction, after its events are inserted: opens a row for each
 * approval_requested and consumes the row of each answer. A row that is not open, or bound to
 * another branch or call, rolls the append back. A challenge with no row (one imported with
 * its log) is checked by the log's rules alone.
 */
export async function recordApprovals(
  tx: Tx,
  branch: BranchRow,
  added: readonly ChainEvent[],
  now: number,
): Promise<Result<void, LogError>> {
  for (const line of added) {
    if (line.kind !== "event") continue;
    const e = line.event;
    if (e.type === "approval_requested") await open(tx, branch, e.data);
    if (e.type !== "approval_granted" && e.type !== "approval_denied") continue;
    const consumed = await consume(tx, branch, e, now);
    if (!consumed.ok) return consumed;
  }
  return ok(undefined);
}

/**
 * The approval rows a verified log implies, from its settlement events alone, never the clock
 * (import): a challenge with a later answer is that answer's state, one without is open, and an
 * expired one is still refused when someone decides it.
 */
export async function projectApprovals(
  tx: Tx,
  branch: Owner,
  events: readonly KnownEvent[],
): Promise<void> {
  for (const e of events) {
    if (e.type === "approval_requested") await open(tx, branch, e.data);
    if (e.type === "approval_granted" || e.type === "approval_denied")
      await decide(tx, e, e.time);
  }
}

type Owner = Pick<BranchRow, "tenant_id" | "thread_id" | "branch_id">;

async function open(
  tx: Tx,
  branch: Owner,
  data: Extract<KnownEvent, { type: "approval_requested" }>["data"],
): Promise<void> {
  await tx.run(
    `INSERT INTO approvals (challenge_id, tenant_id, installation_id, thread_id, branch_id,
      call_id, args_hash, file_hashes, expires_at, state, decided_by, decided_at)
      VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, 'open', NULL, NULL)`,
    [
      data.challenge_id,
      branch.tenant_id,
      branch.thread_id,
      branch.branch_id,
      data.call_id,
      data.args_hash,
      new TextEncoder().encode("[]"),
      data.expires_at,
    ],
  );
}

async function consume(
  tx: Tx,
  branch: BranchRow,
  e: Answer,
  now: number,
): Promise<Result<void, LogError>> {
  const id = e.data.challenge_id;
  const row = await approvalRow(tx, id);
  if (!row.ok) return row;
  if (row.value === undefined) return ok(undefined);
  const refused = refusal(row.value, branch, e, now);
  if (refused !== undefined) return err(refused);
  await decide(tx, e, now);
  return ok(undefined);
}

async function decide(tx: Tx, e: Answer, at: number): Promise<void> {
  await tx.run(
    "UPDATE approvals SET state = ?, decided_by = ?, decided_at = ? WHERE challenge_id = ? AND state = 'open'",
    [
      e.type === "approval_granted" ? "granted" : "denied",
      principalKey(e.actor.principal),
      at,
      e.data.challenge_id,
    ],
  );
}

function refusal(
  row: ApprovalRow,
  branch: BranchRow,
  e: Answer,
  now: number,
): LogError | undefined {
  const id = e.data.challenge_id;
  if (row.state !== "open")
    return logError(
      row.state === "expired" ? "approval_expired" : "approval_duplicate",
      `challenge ${id} is ${row.state}`,
    );
  if (
    row.branch_id !== branch.branch_id ||
    row.call_id !== e.data.call_id ||
    row.args_hash !== e.data.args_hash
  )
    return logError(
      "approval_mismatch",
      `${e.type} does not match challenge ${id}`,
    );
  return row.expires_at <= now
    ? logError("approval_expired", `challenge ${id} expired`)
    : undefined;
}
