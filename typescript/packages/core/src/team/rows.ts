import { z } from "zod";
import {
  BranchId,
  MailEnvelope,
  MemberName,
  type MemberRef,
  PosInt,
  StoredMemberResult,
  TeamId,
  ThreadId,
} from "../log";
import type { EnumOf, Strict } from "../log/zod-types";
import type { Tx } from "../store/driver";

// Typed reads of the team index inside a decided append's transaction (store.sql, Teams). The
// rows are a projection of the logs the store already verified, so a row that fails its schema is
// a broken store invariant: it throws, never a value a caller branches on.

const utf8 = new TextDecoder();
const json = (bytes: Uint8Array): unknown => JSON.parse(utf8.decode(bytes));

const TeamRow: Strict<{
  team_id: typeof TeamId;
  tenant_id: z.ZodString;
  lead_thread_id: typeof ThreadId;
  team_log_branch_id: typeof BranchId;
  closed_at: z.ZodNullable<z.ZodInt>;
}> = z.strictObject({
  team_id: TeamId,
  tenant_id: z.string(),
  lead_thread_id: ThreadId,
  team_log_branch_id: BranchId,
  closed_at: z.int().nullable(),
});
export type TeamRow = z.infer<typeof TeamRow>;

const MEMBER_STATES = [
  "starting",
  "running",
  "idle",
  "parked",
  "ended",
] as const;
const MemberRow: Strict<{
  team_id: typeof TeamId;
  name: typeof MemberName;
  generation: typeof PosInt;
  role: EnumOf<readonly ["lead", "member"]>;
  agent: z.ZodString;
  config_hash: z.ZodString;
  thread_id: typeof ThreadId;
  branch_id: z.ZodNullable<typeof BranchId>;
  state: EnumOf<typeof MEMBER_STATES>;
}> = z.strictObject({
  team_id: TeamId,
  name: MemberName,
  generation: PosInt,
  role: z.enum(["lead", "member"]),
  agent: z.string(),
  config_hash: z.string(),
  thread_id: ThreadId,
  branch_id: BranchId.nullable(),
  state: z.enum(MEMBER_STATES),
});
export type MemberRow = z.infer<typeof MemberRow>;

const MailRow = z
  .strictObject({ envelope: z.instanceof(Uint8Array) })
  .transform((r) => MailEnvelope.parse(json(r.envelope)));

const MonitorRow: Strict<{
  monitor_id: z.ZodString;
  watcher_branch_id: typeof BranchId;
  kind: EnumOf<readonly ["end", "settle", "task"]>;
}> = z.strictObject({
  monitor_id: z.string(),
  watcher_branch_id: BranchId,
  kind: z.enum(["end", "settle", "task"]),
});
export type MonitorRow = z.infer<typeof MonitorRow>;

const MEMBER_COLUMNS =
  "team_id, name, generation, role, agent, config_hash, thread_id, branch_id, state";

export async function teamRow(
  tx: Tx,
  team: string,
): Promise<TeamRow | undefined> {
  return z
    .array(TeamRow)
    .parse(
      await tx.all(
        "SELECT team_id, tenant_id, lead_thread_id, team_log_branch_id, closed_at FROM teams WHERE team_id = ?",
        [team],
      ),
    )[0];
}

/** The team whose log is `branch`, if it is a team log. */
export async function teamOfLog(
  tx: Tx,
  branch: string,
): Promise<TeamRow | undefined> {
  return z
    .array(TeamRow)
    .parse(
      await tx.all(
        "SELECT team_id, tenant_id, lead_thread_id, team_log_branch_id, closed_at FROM teams WHERE team_log_branch_id = ?",
        [branch],
      ),
    )[0];
}

/** Every member row of the team, the lead's included, in (name, generation) order. */
export async function memberRows(
  tx: Tx,
  team: string,
): Promise<readonly MemberRow[]> {
  return z
    .array(MemberRow)
    .parse(
      await tx.all(
        `SELECT ${MEMBER_COLUMNS} FROM team_members WHERE team_id = ? ORDER BY name, generation`,
        [team],
      ),
    );
}

/** The member's current row: its highest generation. */
export async function memberNamed(
  tx: Tx,
  team: string,
  name: string,
): Promise<MemberRow | undefined> {
  return (await memberRows(tx, team)).findLast((r) => r.name === name);
}

/** The rows a thread's own events write: a member's, a lead's, both for a nested lead. */
export async function ownRows(
  tx: Tx,
  thread: ThreadId,
): Promise<readonly MemberRow[]> {
  return z
    .array(MemberRow)
    .parse(
      await tx.all(
        `SELECT ${MEMBER_COLUMNS} FROM team_members WHERE thread_id = ? ORDER BY role, team_id`,
        [thread],
      ),
    );
}

/** A member row as the ref every API and envelope carries. */
export function refOf(team: TeamRow, row: MemberRow): MemberRef {
  return {
    tenant: team.tenant_id,
    team: row.team_id,
    name: row.name,
    generation: row.generation,
  };
}

/** Pending mail to a member's name (or to the team log), in (created_at, mail_id) order. */
export async function pendingTo(
  tx: Tx,
  team: string,
  name: string | null,
): Promise<readonly MailEnvelope[]> {
  return z.array(MailRow).parse(
    await tx.all(
      `SELECT envelope FROM mail WHERE team_id = ? AND to_name IS NOT DISTINCT FROM ? AND state = 'pending'
          ORDER BY created_at, mail_id`,
      [team, name],
    ),
  );
}

/** Pending mail to a writer: its thread's own rows, or the team log when it is one. */
export async function pendingHere(
  tx: Tx,
  thread: ThreadId,
  branch: string,
): Promise<readonly MailEnvelope[]> {
  const rows = await ownRows(tx, thread);
  if (rows.length > 0) return await pendingFor(tx, rows);
  const team = await teamOfLog(tx, branch);
  return team === undefined ? [] : await pendingTo(tx, team.team_id, null);
}

/** Pending mail to any of a thread's own rows (a nested lead has two), in one order. */
export async function pendingFor(
  tx: Tx,
  rows: readonly MemberRow[],
): Promise<readonly MailEnvelope[]> {
  if (rows.length === 0) return [];
  const pairs = rows.map(() => "(?, ?)").join(", ");
  return z.array(MailRow).parse(
    await tx.all(
      `SELECT envelope FROM mail WHERE state = 'pending' AND (team_id, to_name) IN (VALUES ${pairs})
        ORDER BY created_at, mail_id`,
      rows.flatMap((r) => [r.team_id, r.name]),
    ),
  );
}

/** A mail row's envelope, whatever its state. */
export async function mailEnvelope(
  tx: Tx,
  mailId: string,
): Promise<MailEnvelope | undefined> {
  return z
    .array(MailRow)
    .parse(
      await tx.all("SELECT envelope FROM mail WHERE mail_id = ?", [mailId]),
    )[0];
}

const SettledRow = z
  .strictObject({ result: z.instanceof(Uint8Array), updated_seq: PosInt })
  .transform((r) => ({
    result: StoredMemberResult.parse(json(r.result)),
    seq: r.updated_seq,
  }));

/**
 * An idle or ended member's committed result and the seq of the member_idle or member_ended
 * that wrote it (nothing else changes the row until its next turn opens).
 */
export async function settledOf(
  tx: Tx,
  row: MemberRow,
): Promise<{ readonly result: StoredMemberResult; readonly seq: number }> {
  const [got] = z
    .array(SettledRow)
    .parse(
      await tx.all(
        "SELECT result, updated_seq FROM team_members WHERE team_id = ? AND name = ? AND generation = ?",
        [row.team_id, row.name, row.generation],
      ),
    );
  if (got === undefined) throw new Error(`no settled row ${row.name}`);
  return got;
}

const AskRow: Strict<{
  asker_branch_id: typeof BranchId;
  deadline: z.ZodInt;
  state: z.ZodString;
}> = z.strictObject({
  asker_branch_id: BranchId,
  deadline: z.int(),
  state: z.string(),
});
export type AskRow = z.infer<typeof AskRow>;

/** An ask's row, whatever its state. */
export async function askRow(
  tx: Tx,
  askId: string,
): Promise<AskRow | undefined> {
  return z
    .array(AskRow)
    .parse(
      await tx.all(
        "SELECT asker_branch_id, deadline, state FROM asks WHERE ask_id = ?",
        [askId],
      ),
    )[0];
}

/** This branch's open asks whose deadline is at or before `now`, oldest first. */
/** This branch's open asks, in ask_id order. */
export async function openAsks(
  tx: Tx,
  branch: string,
): Promise<readonly string[]> {
  return z
    .array(z.strictObject({ ask_id: z.string() }))
    .parse(
      await tx.all(
        "SELECT ask_id FROM asks WHERE asker_branch_id = ? AND state = 'open' ORDER BY ask_id",
        [branch],
      ),
    )
    .map((r) => r.ask_id);
}

export async function dueAsks(
  tx: Tx,
  branch: string,
  now: number,
): Promise<readonly string[]> {
  return z
    .array(z.strictObject({ ask_id: z.string() }))
    .parse(
      await tx.all(
        `SELECT ask_id FROM asks WHERE asker_branch_id = ? AND state = 'open' AND deadline <= ?
          ORDER BY deadline, ask_id`,
        [branch, now],
      ),
    )
    .map((r) => r.ask_id);
}

/** The live monitors on one member generation, in monitor_id order. */
export async function monitorsOn(
  tx: Tx,
  team: string,
  row: MemberRow,
): Promise<readonly MonitorRow[]> {
  return z.array(MonitorRow).parse(
    await tx.all(
      `SELECT monitor_id, watcher_branch_id, kind FROM monitors
          WHERE team_id = ? AND target_name = ? AND target_generation = ? ORDER BY monitor_id`,
      [team, row.name, row.generation],
    ),
  );
}
