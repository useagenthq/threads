import type { EventOf } from "../fold/state";
import {
  type KnownEvent,
  type MailEnvelope,
  type MemberRef,
  principalKey,
  type TeamId,
} from "../log";
import type { Tx } from "../store/driver";
import { jsonBytes } from "./json";
import { inScope, SCOPED, type Scope, scoped, type TeamLog } from "./scope";

// The rows a sender's or starter's append inserts (the replay rule's first pass).

/** The rows `events`, appended to `log`, insert. */
export async function insertRows(
  tx: Tx,
  log: TeamLog,
  events: readonly KnownEvent[],
  scope?: TeamId,
): Promise<void> {
  for (const e of events) await insertOne(tx, log, e, scope);
}

async function insertOne(
  tx: Tx,
  log: TeamLog,
  e: KnownEvent,
  scope: Scope,
): Promise<void> {
  if (e.type === "team_opened" && inScope(scope, e.data.team))
    await tx.run(
      "INSERT INTO teams (team_id, tenant_id, lead_thread_id, team_log_branch_id, closed_at) VALUES (?, ?, ?, ?, NULL)",
      [e.data.team, e.data.lead.tenant, e.data.lead_thread_id, log.branchId],
    );
  else if (e.type === "thread_started" && e.data.team !== undefined)
    await insertLead(tx, log, e, scope);
  else if (e.type === "member_started") await insertMember(tx, log, e, scope);
  else if (e.type === "message_sent")
    await insertMail(tx, log, e, e.data.envelope, scope);
  else if (e.type === "operator_request")
    await insertReceipt(tx, log, e, scope);
  else if (e.type === "wait_started")
    for (const m of e.data.members)
      await insertMonitor(tx, log, e, m, "settle", e.data.wait_id, scope);
  else if (e.type === "monitor_set")
    await insertMonitor(tx, log, e, e.data.member, "end", null, scope);
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

async function insertRow(tx: Tx, row: MemberRow): Promise<void> {
  await tx.run(
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
async function insertLead(
  tx: Tx,
  log: TeamLog,
  e: EventOf<"thread_started">,
  scope: Scope,
): Promise<void> {
  const team = e.data.team?.id;
  if (team === undefined || !inScope(scope, team)) return;
  await insertRow(tx, {
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
async function insertMember(
  tx: Tx,
  log: TeamLog,
  e: EventOf<"member_started">,
  scope: Scope,
): Promise<void> {
  const m = e.data.member;
  if (!inScope(scope, m.team)) return;
  await insertRow(tx, {
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
  await insertMonitor(tx, log, e, m, "task", null, scope);
}

/** A monitor's id is derived: `<watcher branch>:<registering event_id>:<target name or task>`. */
async function insertMonitor(
  tx: Tx,
  log: TeamLog,
  e: KnownEvent,
  target: MemberRef,
  kind: "end" | "settle" | "task",
  waitId: string | null,
  scope: Scope,
): Promise<void> {
  if (!inScope(scope, target.team)) return;
  const name = kind === "task" ? "task" : target.name;
  await tx.run(
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

async function insertMail(
  tx: Tx,
  log: TeamLog,
  e: EventOf<"message_sent">,
  env: MailEnvelope,
  scope: Scope,
): Promise<void> {
  if (!inScope(scope, env.team)) return;
  const to = env.to === "team_log" ? undefined : env.to;
  const root = env.provenance.root_request;
  await tx.run(
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
    await tx.run(
      `INSERT INTO asks (ask_id, team_id, asker_branch_id, recipient_name, recipient_generation,
         deadline, state, closed_seq) VALUES (?, ?, ?, ?, ?, ?, 'open', NULL)`,
      [
        env.ask_id ?? null,
        env.team,
        log.branchId,
        to.name,
        to.generation,
        env.deadline ?? null,
      ],
    );
}

/** An operator request with an idempotency key, in the team log whose teams row names it. */
async function insertReceipt(
  tx: Tx,
  log: TeamLog,
  e: EventOf<"operator_request">,
  scope: Scope,
): Promise<void> {
  const d = e.data;
  if (d.idempotency_key === undefined) return;
  await tx.run(
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
