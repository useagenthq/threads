import { z } from "zod";
import { generationKey } from "../fold/host";
import type { Fold } from "../fold/state";
import { MemberName, parseLogLine, type ThreadId } from "../log";
import type { Tx } from "../store/driver";
import { parseRows } from "../store/tables";
import { restartsInWindow } from "../validate/supervision";
import type { Batch } from "./batch";
import { type Refused, refusal } from "./call";
import type { Started } from "./ops";
import type { Request } from "./request";
import { type MemberRow, memberNamed, type TeamRow } from "./rows";

// The supervisor step (spec/schema/README.md, "Teams Phase 2" E, semantic rule 51): the host team
// log's writer decides once on each ended host member generation, and a restart starts the next
// one in the same append. Reference: spec/tools/fixtures/ops_supervise.py::supervise.
//
// Exactly once is the fold inside the append's transaction: `decided` holds every generation this
// log has already decided, so a second host that reaches the same end appends nothing. A host that
// decided from a fold that went stale is refused by rule 51 and rolls back.

/** members.<name> as the host resolved it (spec/api.json HostMemberOptions), on the wire. */
export type RestartPolicy = {
  readonly restart: "on_failure" | "never";
  readonly max_restarts: number;
  readonly within_ms: number;
};

export type Supervised =
  | { readonly status: "nothing_ended" }
  | { readonly status: "already_decided" }
  | {
      readonly status: "decided";
      readonly action: "restart" | "stop";
      readonly restarts_in_window: number;
    };

/** What the decision reads and writes, inside the host team log's own append. */
export type SuperviseContext = {
  readonly tx: Tx;
  /** The team log's committed fold: its `host.decided` is what makes the decision exactly once. */
  readonly fold: Fold;
  readonly batch: Batch;
  readonly team: TeamRow;
  /** The next generation's thread, minted before the append; unused by a stop. */
  readonly threadId: ThreadId;
  /**
   * The agent's pin as the host holds it now, stored before this append. A restart re-pins, so
   * an agent that was unregistered or changed comes back on the definition that is live: that is
   * what makes a restart a recovery and not a repeat. Absent (the op vectors, which have no
   * registry, and a name the host no longer defines): the ended generation's own pin, which
   * fails its rebind again and stops the member at the cap.
   */
  readonly configHash?: string | undefined;
};

/**
 * One decision on the named host member's ended generation. `restart` needs the policy to allow
 * it, the window to hold fewer restarts than the cap, and that end to be `failed`: a rebind
 * failure. An own-budget end and a cancel are stops, and `restart: "never"` still records one.
 */
export async function supervise(
  ctx: SuperviseContext,
  name: string,
  policy: RestartPolicy,
): Promise<Supervised> {
  const row = await memberNamed(ctx.tx, ctx.team.team_id, name);
  if (row?.role !== "host_member" || row.state !== "ended")
    return { status: "nothing_ended" };
  const { host } = ctx.fold.team;
  if (host.decided.has(generationKey(name, row.generation)))
    return { status: "already_decided" };
  const end = await endedAt(ctx.tx, row.branch_id);
  const count = restartsInWindow(host, name, ctx.batch.now, policy.within_ms);
  const allowed =
    policy.restart === "on_failure" && count < policy.max_restarts;
  const action = allowed && end.failed ? "restart" : "stop";
  const member = {
    tenant: ctx.team.tenant_id,
    team: ctx.team.team_id,
    name: MemberName.parse(name),
    generation: row.generation,
  };
  ctx.batch.add({
    type: "supervisor_decided",
    type_version: 1,
    critical: true,
    actor: { kind: "host" },
    data: {
      member,
      ended: { branch_id: end.branch, seq: end.seq },
      action,
      restarts_in_window: count,
      policy,
    },
  });
  if (action === "restart")
    ctx.batch.add({
      type: "member_started",
      type_version: 1,
      critical: true,
      actor: { kind: "host" },
      data: {
        member: { ...member, generation: row.generation + 1 },
        agent: row.agent,
        config_hash: ctx.configHash ?? row.config_hash,
        thread_id: ctx.threadId,
        host_member: true,
        restart_of: row.generation,
      },
    });
  return { status: "decided", action, restarts_in_window: count };
}

const EndRow = z.strictObject({
  branch_id: z.string(),
  seq: z.int(),
  line: z.instanceof(Uint8Array),
});

const utf8 = new TextDecoder();

/** The member_ended of an ended generation's own log: where it is, and whether it failed. */
async function endedAt(
  tx: Tx,
  branch: string | null,
): Promise<{ branch: string; seq: number; failed: boolean }> {
  const rows = parseRows(
    EndRow,
    await tx.all(
      `SELECT branch_id, seq, line FROM events
        WHERE branch_id = ? AND type = 'member_ended' ORDER BY seq DESC LIMIT 1`,
      [branch],
    ),
  );
  const row = rows.ok ? rows.value[0] : undefined;
  const parsed =
    row === undefined ? undefined : parseLogLine(utf8.decode(row.line));
  if (row === undefined || parsed === undefined || !parsed.ok)
    throw new Error(`an ended member has a member_ended: branch ${branch}`);
  const line = parsed.value;
  if (line.kind !== "event" || line.event.type !== "member_ended")
    throw new Error(`an ended member has a member_ended: branch ${branch}`);
  return {
    branch: row.branch_id,
    seq: row.seq,
    failed: line.event.data.result.status === "failed",
  };
}

/**
 * The operator's half of rule 51: `Team.start(name)` on a host team starts the next generation of
 * a host member the supervisor stopped (spec/api.json Team.start, "Teams Phase 2" E). Anything
 * else — a name that is no host member of this team, a generation that is still live, or one the
 * supervisor restarted rather than stopped — is `forbidden`, and the request records the refusal.
 */
export async function restart(
  req: Request,
  name: string,
  fold: Fold,
  threadId: ThreadId,
  configHash?: string,
): Promise<Started | Refused> {
  const denied = req.decide("start", name);
  if (denied !== undefined) return req.refuse(denied);
  const row = await memberNamed(req.tx, req.team.team_id, name);
  if (!stopped(row, fold)) return req.refuse(refusal("forbidden"));
  const member = {
    tenant: req.team.tenant_id,
    team: req.team.team_id,
    name: MemberName.parse(name),
    generation: row.generation + 1,
  };
  req.batch.add({
    type: "member_started",
    type_version: 1,
    critical: true,
    actor: { kind: "host" },
    data: {
      member,
      agent: row.agent,
      config_hash: configHash ?? row.config_hash,
      thread_id: threadId,
      host_member: true,
      restart_of: row.generation,
      // Rule 51: an operator restart's provenance names the operator_request that asked for it.
      provenance: req.provenance,
    },
  });
  return req.done({ member, status: "started" } satisfies Started);
}

/** The name is a host member of this team whose current generation the supervisor stopped. */
function stopped(row: MemberRow | undefined, fold: Fold): row is MemberRow {
  return (
    row?.role === "host_member" &&
    row.state === "ended" &&
    fold.team.host.decided.get(generationKey(row.name, row.generation)) ===
      "stop"
  );
}
