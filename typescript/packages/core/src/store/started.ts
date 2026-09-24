import { z } from "zod";
import type { EventOf } from "../fold/state";
import { BranchId, parseLogLine, ThreadId } from "../log";
import { ok, type Result } from "../result";
import type { LogError } from "../verify/error";
import type { SqliteDriver } from "./driver";
import { parseRows } from "./tables";

/** What opened a thread's main branch: a thread_started, or a team log's team_opened. */
export type Opened = {
  readonly threadId: ThreadId;
  readonly branchId: BranchId;
  readonly event: EventOf<"thread_started"> | EventOf<"team_opened">;
};

const FirstRow = z.strictObject({
  thread_id: ThreadId,
  branch_id: BranchId,
  line: z.instanceof(Uint8Array),
});

const utf8 = new TextDecoder();

/**
 * The first event of every main branch of `tenantId`: how deletion and the team index find a
 * thread's parent, the team it leads, and the team logs. A first line that doesn't parse links
 * nothing, so its thread stays deletable on its own.
 */
export function openedThreads(
  db: SqliteDriver,
  tenantId: string,
): Result<readonly Opened[], LogError> {
  const rows = parseRows(
    FirstRow,
    db.all(
      `SELECT b.thread_id, b.branch_id, e.line FROM events e
        JOIN branches b ON b.branch_id = e.branch_id
        WHERE b.tenant_id = ? AND b.parent_branch_id IS NULL AND e.seq = 1
          AND e.type IN ('thread_started', 'team_opened')`,
      [tenantId],
    ),
  );
  if (!rows.ok) return rows;
  const opened = rows.value.flatMap((row): Opened[] => {
    const parsed = parseLogLine(utf8.decode(row.line));
    if (!parsed.ok || parsed.value.kind !== "event") return [];
    const { event } = parsed.value;
    return event.type === "thread_started" || event.type === "team_opened"
      ? [{ threadId: row.thread_id, branchId: row.branch_id, event }]
      : [];
  });
  return ok(opened);
}
