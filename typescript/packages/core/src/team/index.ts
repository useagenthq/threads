import { apply } from "../fold/apply";
import { type EventLine, type EventOf, emptyFold } from "../fold/state";
import {
  type BranchId,
  type KnownEvent,
  type MailEnvelope,
  type MemberRef,
  principalKey,
  type TeamId,
  type ThreadId,
} from "../log";
import type { SqliteDriver, SqlValue } from "../store/driver";
import { jsonBytes } from "./json";

// The team index (spec/schema/README.md, "Teams", the replay rule): every row of store.sql's
// team tables is inserted or changed by the append that holds its bytes. insertRows is what a
// sender's or starter's append inserts, changeRows what each recipient's and member's own
// events change. A writer calls both for each append (insert first); a rebuild calls insertRows
// for every log, then changeRows for every log, so it doesn't depend on the order logs are read.
// Ported from spec/tools/fixtures/team_index.py.

/** The log an append belongs to. */
export type TeamLog = {
  readonly threadId: ThreadId;
  readonly branchId: BranchId;
};

/** Limits every row write to one team: a rebuild must not touch a nested team's rows. */
type Scope = TeamId | undefined;

const inScope = (scope: Scope, team: string): boolean =>
  scope === undefined || scope === team;

/** `AND` clause and params that keep an update or delete inside the scope. */
const SCOPED = "AND (? IS NULL OR team_id = ?)";
const scoped = (scope: Scope): readonly SqlValue[] => [
  scope ?? null,
  scope ?? null,
];

/** A member's own events that write its row's state (a turn opener writes running). */
const STATES: Readonly<Record<string, string>> = {
  parked: "parked",
  resumed: "running",
  member_idle: "idle",
  member_ended: "ended",
};

/** The event ids at which the reducer opens a turn, re-folded from the log's lines. */
export function turnOpeners(lines: readonly EventLine[]): ReadonlySet<string> {
  const fold = emptyFold();
  const opened = new Set<string>();
  for (const line of lines) {
    const before = fold.turnOpen;
    apply(fold, line);
    if (!before && fold.turnOpen) opened.add(line.event.event_id);
  }
  return opened;
}

/** The rows `events`, appended to `log`, insert. */
export function insertRows(
  db: SqliteDriver,
  log: TeamLog,
  events: readonly KnownEvent[],
  scope?: TeamId,
): void {
  for (const e of events) insertOne(db, log, e, scope);
}

function insertOne(
  db: SqliteDriver,
  log: TeamLog,
  e: KnownEvent,
  scope: Scope,
): void {
  if (e.type === "team_opened" && inScope(scope, e.data.team))
    db.run(
      "INSERT INTO teams (team_id, tenant_id, lead_thread_id, team_log_branch_id, closed_at) VALUES (?, ?, ?, ?, NULL)",
      [e.data.team, e.data.lead.tenant, e.data.lead_thread_id, log.branchId],
    );
  else if (e.type === "thread_started" && e.data.team !== undefined)
    insertLead(db, log, e, scope);
  else if (e.type === "member_started") insertMember(db, log, e, scope);
  else if (e.type === "message_sent")
    insertMail(db, log, e, e.data.envelope, scope);
  else if (e.type === "operator_request") insertReceipt(db, log, e, scope);
  else if (e.type === "wait_started")
    for (const m of e.data.members)
      insertMonitor(db, log, e, m, "settle", e.data.wait_id, scope);
  else if (e.type === "monitor_set")
    insertMonitor(db, log, e, e.data.member, "end", null, scope);
  else if (e.type === "agent_spawned" && e.data.mode === "background")
    db.run(
      "INSERT INTO pending_wakes (branch_id, child_thread_id) VALUES (?, ?)",
      [log.branchId, e.data.child_thread_id],
    );
}

type MemberRow = {
  readonly team: string;
  readonly name: string;
  readonly generation: number;
  readonly role: "lead" | "member";
  readonly agent: string;
  readonly configHash: string;
  readonly threadId: string;
  readonly branchId: string | null;
  readonly provenance: unknown;
  readonly seq: number;
};

function insertRow(db: SqliteDriver, row: MemberRow): void {
  db.run(
    `INSERT INTO team_members (team_id, name, generation, role, agent, config_hash, thread_id,
       branch_id, provenance, state, result, updated_seq) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)`,
    [
      row.team,
      row.name,
      row.generation,
      row.role,
      row.agent,
      row.configHash,
      row.threadId,
      row.branchId,
      row.provenance === undefined ? null : jsonBytes(row.provenance),
      row.branchId === null ? "starting" : "running",
      row.seq,
    ],
  );
}

/** The lead's row, from its own thread_started{team}: generation 1, no provenance. */
function insertLead(
  db: SqliteDriver,
  log: TeamLog,
  e: EventOf<"thread_started">,
  scope: Scope,
): void {
  const team = e.data.team?.id;
  if (team === undefined || !inScope(scope, team)) return;
  insertRow(db, {
    team,
    name: e.data.agent_name,
    generation: 1,
    role: "lead",
    agent: e.data.agent_name,
    configHash: e.data.config_hash,
    threadId: log.threadId,
    branchId: log.branchId,
    provenance: undefined,
    seq: e.seq,
  });
}

/** A member's row, in the starting window (no branch yet), and its starter's task monitor. */
function insertMember(
  db: SqliteDriver,
  log: TeamLog,
  e: EventOf<"member_started">,
  scope: Scope,
): void {
  const m = e.data.member;
  if (!inScope(scope, m.team)) return;
  insertRow(db, {
    team: m.team,
    name: m.name,
    generation: m.generation,
    role: "member",
    agent: e.data.agent,
    configHash: e.data.config_hash,
    threadId: e.data.thread_id,
    branchId: null,
    provenance: e.data.provenance,
    seq: e.seq,
  });
  insertMonitor(db, log, e, m, "task", null, scope);
}

/** A monitor's id is derived: `<watcher branch>:<registering event_id>:<target name or task>`. */
function insertMonitor(
  db: SqliteDriver,
  log: TeamLog,
  e: KnownEvent,
  target: MemberRef,
  kind: "end" | "settle" | "task",
  waitId: string | null,
  scope: Scope,
): void {
  if (!inScope(scope, target.team)) return;
  const name = kind === "task" ? "task" : target.name;
  db.run(
    `INSERT INTO monitors (monitor_id, team_id, watcher_branch_id, target_name, target_generation,
       kind, wait_id) VALUES (?, ?, ?, ?, ?, ?, ?)`,
    [
      `${log.branchId}:${e.event_id}:${name}`,
      target.team,
      log.branchId,
      target.name,
      target.generation,
      kind,
      waitId,
    ],
  );
}

function insertMail(
  db: SqliteDriver,
  log: TeamLog,
  e: EventOf<"message_sent">,
  env: MailEnvelope,
  scope: Scope,
): void {
  if (!inScope(scope, env.team)) return;
  const to = env.to === "team_log" ? undefined : env.to;
  const root = env.provenance.root_request;
  db.run(
    `INSERT INTO mail (mail_id, team_id, kind, to_name, to_generation, principal_key, root_request,
       envelope, created_at, state, claim_token, claim_expires_at, consumed_seq)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', NULL, NULL, NULL)`,
    [
      env.mail_id,
      env.team,
      env.kind,
      to?.name ?? null,
      to?.generation ?? null,
      principalKey(env.provenance.principal),
      `${root.thread_id}:${root.event_id}`,
      jsonBytes(env),
      e.time,
    ],
  );
  if (env.kind === "ask" && to !== undefined)
    db.run(
      `INSERT INTO asks (ask_id, team_id, asker_branch_id, recipient_name, recipient_generation,
         deadline, state, closed_seq) VALUES (?, ?, ?, ?, ?, ?, 'open', NULL)`,
      [
        // An ask's id is its own mail id (semantic rule 44).
        env.mail_id,
        env.team,
        log.branchId,
        to.name,
        to.generation,
        env.deadline ?? null,
      ],
    );
}

/** An operator request with an idempotency key, in the team log whose teams row names it. */
function insertReceipt(
  db: SqliteDriver,
  log: TeamLog,
  e: EventOf<"operator_request">,
  scope: Scope,
): void {
  const d = e.data;
  if (d.idempotency_key === undefined) return;
  db.run(
    `INSERT INTO operator_receipts (tenant_id, team_id, op, idempotency_key, principal_key,
       body_hash, request_id)
       SELECT tenant_id, team_id, ?, ?, ?, ?, ? FROM teams WHERE team_log_branch_id = ? ${SCOPED}`,
    [
      d.op,
      d.idempotency_key,
      principalKey(d.principal),
      d.body_hash,
      d.request_id,
      log.branchId,
      ...scoped(scope),
    ],
  );
}

/** The rows `log`'s own `events` change; `opened` are the events that opened a turn. */
export function changeRows(
  db: SqliteDriver,
  log: TeamLog,
  events: readonly KnownEvent[],
  opened: ReadonlySet<string>,
  scope?: TeamId,
): void {
  for (const e of events) {
    moveRows(db, log, e, scope);
    changeOwnRows(db, log, e, opened.has(e.event_id), scope);
  }
}

/** Mail, asks, monitors and wake rows another log inserted, moved by this log's event. */
function moveRows(
  db: SqliteDriver,
  log: TeamLog,
  e: KnownEvent,
  scope: Scope,
): void {
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
  else moveMonitorsAndWakes(db, log, e, at);
}

function moveMonitorsAndWakes(
  db: SqliteDriver,
  log: TeamLog,
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
  const child =
    e.type === "agent_finished"
      ? e.data.child_thread_id
      : e.type === "parked" && e.data.address.kind === "child"
        ? e.data.address.id
        : undefined;
  if (child !== undefined)
    db.run(
      "DELETE FROM pending_wakes WHERE branch_id = ? AND child_thread_id = ?",
      [log.branchId, child],
    );
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
