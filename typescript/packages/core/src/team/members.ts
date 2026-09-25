import { z } from "zod";
import type { EventOf } from "../fold/state";
import { BranchId, type KnownEvent, PosInt, ThreadId } from "../log";
import { knownEvents } from "../reduce";
import { err, ok, type Result } from "../result";
import type { LogStore } from "../store";
import { READ_ONLY } from "../store/driver";
import { parseRows } from "../store/tables";
import { type ReadError, readError, readLog } from "../thread/read";
import type { VerifiedLog } from "../verify";

// A lead's members as the tree walks see them (spec/schema/README.md, "Tree walks with teams"):
// its team's `role = member` rows, every generation, model- or operator-started. The lead's own
// row is never a child: the walk started at the lead, or reached it through its own parent.

/** One member of a lead's team, and how to read it on the walk's turn. */
export type TeamMember = {
  readonly name: string;
  readonly generation: number;
  readonly threadId: ThreadId;
  /**
   * The member's verified log, its backlink checked; `pending` in the starting window (no
   * branch, and its starter has no task notification for it); else log_corrupt.
   */
  readonly open: () => Promise<Result<VerifiedLog | "pending", ReadError>>;
};

const MemberRow = z.strictObject({
  name: z.string(),
  generation: PosInt,
  thread_id: ThreadId,
  branch_id: BranchId.nullable(),
});
type MemberRow = z.infer<typeof MemberRow>;

type Started = EventOf<"thread_started">;
type MemberStarted = EventOf<"member_started">;

const NOTICES: ReadonlySet<string> = new Set([
  "member_settled",
  "member_ended",
]);

/** The members of the team `lead` leads, in (name, generation) order; none if it leads none. */
export async function teamMembers(
  store: LogStore,
  lead: VerifiedLog,
): Promise<Result<readonly TeamMember[], ReadError>> {
  const events = knownEvents(lead);
  const started = events.find((e): e is Started => e.type === "thread_started");
  const team = started?.data.team;
  if (started === undefined || team === undefined) return ok([]);
  const rows = parseRows(
    MemberRow,
    await store.driver.transaction(
      (tx) =>
        tx.all(
          `SELECT m.name, m.generation, m.thread_id, m.branch_id FROM team_members m
            JOIN teams t ON t.team_id = m.team_id
            WHERE m.team_id = ? AND t.tenant_id = ? AND m.role = 'member'
            ORDER BY m.name, m.generation`,
          [team.id, store.tenant],
        ),
      READ_ONLY,
    ),
  );
  if (!rows.ok) return err(readError("log_corrupt", rows.error.message));
  const starts = events.filter(
    (e): e is MemberStarted => e.type === "member_started",
  );
  return ok(
    rows.value.map((row) => {
      const start = starts.find(
        (e) =>
          e.data.member.team === team.id &&
          e.data.member.name === row.name &&
          e.data.member.generation === row.generation,
      );
      const teamLog = async (): Promise<Result<VerifiedLog, ReadError>> =>
        readLog(store, team.log_branch_id);
      return {
        name: row.name,
        generation: row.generation,
        threadId: row.thread_id,
        open: () =>
          row.branch_id === null
            ? starting(row, start, events, teamLog)
            : backlinked(store, row.branch_id, start ?? started),
      };
    }),
  );
}

/**
 * A member with a branch counts once its thread_started names, as its team_member parent, the
 * lead's member_started for it, or the lead's thread_started for an operator start.
 */
async function backlinked(
  store: LogStore,
  branch: BranchId,
  by: MemberStarted | Started,
): Promise<Result<VerifiedLog, ReadError>> {
  const read = await readLog(store, branch);
  if (!read.ok) return read;
  const own = knownEvents(read.value).find(
    (e): e is Started => e.type === "thread_started",
  );
  const link = own?.data.parent;
  return link?.relation === "team_member" &&
    link.thread_id === by.thread_id &&
    link.branch_id === by.branch_id &&
    link.event_id === by.event_id
    ? read
    : err(
        readError(
          "log_corrupt",
          "its thread_started doesn't name the member_started that started it",
        ),
      );
}

/**
 * A member without a branch is in the starting window only while its starter (the lead, or the
 * team log for an operator start) has received no task notification for it.
 */
async function starting(
  row: MemberRow,
  inLead: MemberStarted | undefined,
  leadEvents: readonly KnownEvent[],
  teamLog: () => Promise<Result<VerifiedLog, ReadError>>,
): Promise<Result<"pending", ReadError>> {
  let start = inLead;
  let starter = leadEvents;
  if (start === undefined) {
    const read = await teamLog();
    if (!read.ok) return read;
    starter = knownEvents(read.value);
    start = starter.find(
      (e): e is MemberStarted =>
        e.type === "member_started" &&
        e.data.member.name === row.name &&
        e.data.member.generation === row.generation,
    );
  }
  if (start === undefined)
    return err(readError("log_corrupt", "no member_started names it"));
  const monitor = `${start.branch_id}:${start.event_id}:task`;
  const notified = starter.some(
    (e) =>
      e.type === "message_received" &&
      NOTICES.has(e.data.envelope.kind) &&
      e.data.envelope.monitor_id === monitor,
  );
  return notified
    ? err(
        readError(
          "log_corrupt",
          "its log is missing, though its starter received its task notification",
        ),
      )
    : ok("pending");
}
