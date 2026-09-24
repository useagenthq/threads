import type { KnownEvent, TeamId } from "../log";
import type { SqliteDriver, SqlValue } from "../store/driver";
import { jsonBytes } from "./json";
import { SCOPED, type Scope, scoped, type TeamLog } from "./scope";

// What each recipient's and member's own events change (the replay rule's second pass).

/** A member's own events that write its row's state (a turn opener writes running). */
const STATES: Readonly<Record<string, string>> = {
  parked: "parked",
  resumed: "running",
  member_idle: "idle",
  member_ended: "ended",
};

/** The rows `log`'s own `events` change; `opened` are the events that opened a turn. */
export function changeRows(
  db: SqliteDriver,
  log: TeamLog,
  events: readonly KnownEvent[],
  opened: ReadonlySet<string>,
  scope?: TeamId,
): void {
  for (const e of events) {
    moveRows(db, e, scope);
    changeOwnRows(db, log, e, opened.has(e.event_id), scope);
  }
}

/** Mail, asks and monitors another log inserted, moved by this log's event. */
function moveRows(db: SqliteDriver, e: KnownEvent, scope: Scope): void {
  const at = scoped(scope);
  if (
    e.type === "message_received" ||
    (e.type === "user_input" && e.data.source === "team_task")
  )
    db.run(
      `UPDATE mail SET state = 'consumed', consumed_seq = ? WHERE mail_id = ? ${SCOPED}`,
      [e.seq, e.data.mail_id ?? null, ...at],
    );
  else if (e.type === "mail_refused")
    db.run(
      `UPDATE mail SET state = ?, consumed_seq = ? WHERE mail_id = ? ${SCOPED}`,
      [
        e.data.code === "stale_member" ? "stale" : "returned",
        e.seq,
        e.data.mail_id,
        ...at,
      ],
    );
  else if (e.type === "ask_closed")
    db.run(
      `UPDATE asks SET state = ?, closed_seq = ? WHERE ask_id = ? ${SCOPED}`,
      [e.data.outcome.status, e.seq, e.data.ask_id, ...at],
    );
  else moveMonitors(db, e, at);
}

function moveMonitors(
  db: SqliteDriver,
  e: KnownEvent,
  at: readonly SqlValue[],
): void {
  const fired =
    e.type === "message_sent" && e.data.envelope.kind !== "member_parked"
      ? e.data.envelope.monitor_id
      : undefined;
  const observed = e.type === "member_observed" ? e.data.monitor_id : undefined;
  const monitor = fired ?? observed;
  if (monitor !== undefined)
    db.run(`DELETE FROM monitors WHERE monitor_id = ? ${SCOPED}`, [
      monitor,
      ...at,
    ]);
  else if (e.type === "wait_finished")
    db.run(`DELETE FROM monitors WHERE wait_id = ? ${SCOPED}`, [
      e.data.wait_id,
      ...at,
    ]);
}

/**
 * This log's own team_members rows (the lead's, a member's; both for a nested lead): the
 * member's branch at its thread_started, running at a turn opener, then parked, running, idle
 * or ended with the result; a lead's end closes its team.
 */
function changeOwnRows(
  db: SqliteDriver,
  log: TeamLog,
  e: KnownEvent,
  opens: boolean,
  scope: Scope,
): void {
  const own = [log.threadId, ...scoped(scope)];
  const state = opens ? "running" : STATES[e.type];
  if (e.type === "thread_started" && e.data.parent !== undefined)
    db.run(
      `UPDATE team_members SET branch_id = ?, state = 'running', updated_seq = ? WHERE thread_id = ? ${SCOPED}`,
      [log.branchId, e.seq, ...own],
    );
  else if (state !== undefined)
    db.run(
      `UPDATE team_members SET state = ?, updated_seq = ? WHERE thread_id = ? ${SCOPED}`,
      [state, e.seq, ...own],
    );
  if (e.type !== "member_idle" && e.type !== "member_ended") return;
  db.run(`UPDATE team_members SET result = ? WHERE thread_id = ? ${SCOPED}`, [
    jsonBytes(e.data.result),
    ...own,
  ]);
  if (e.type === "member_ended")
    db.run(
      `UPDATE teams SET closed_at = ? WHERE team_id IN
         (SELECT team_id FROM team_members WHERE thread_id = ? AND role = 'lead' ${SCOPED})`,
      [e.time, ...own],
    );
}
