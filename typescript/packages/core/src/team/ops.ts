import type { z } from "zod";
import { type Budget, MemberName, type MemberRef, type ThreadId } from "../log";
import type { Result } from "../result";
import type { Tx } from "../store/driver";
import { type Refusal, type Refused, refusal } from "./call";
import type { InvalidDefinition, Resolved } from "./dynamic";
import { bodyOf, sent } from "./mail";
import type { Request, Target } from "./request";
import { type MemberRow, memberRows, pendingTo } from "./rows";

// start and send (spec/schema/README.md, "Teams"; design §4.5 and §4.10) for a model call or an
// operator request, each decided inside the request's append: the policy decision, then the op's
// checks in the order the op vectors pin, then its events and the recorded outcome. Reference:
// spec/tools/fixtures/ops_member.py and ops_send.py.

/** The team's limits: agent({teamLimits}). */
export type TeamLimits = {
  readonly concurrent: number;
  readonly mailbox: number;
};

/** An agent the caller's team lists, as start pins it. */
export type Listed = {
  readonly configHash: string;
  readonly budget?: z.infer<typeof Budget>;
};

/** What start reads besides the store: the caller's definition. */
export type StartPlan = {
  readonly agents: ReadonlyMap<string, Listed>;
  /**
   * The start's label and chosen fields, resolved against the named agent (a dynamic agent's
   * template, lane 26). Read only for a listed agent.
   */
  readonly resolved: Result<Resolved, InvalidDefinition>;
  readonly limits: TeamLimits;
  /** Every budget covering the new member has room for one request of its model. */
  /** Read in `tx`, the start's append. */
  readonly headroom: (agent: string, tx: Tx) => Promise<boolean>;
  /** The new member's thread id, minted before the append. */
  readonly threadId: ThreadId;
};

export type Started = {
  readonly member: MemberRef;
  readonly status: "started";
};
export type Sent = { readonly id: string; readonly status: "sent" };

const LIVE: ReadonlySet<string> = new Set(["starting", "running"]);

/** member.start: member_started and its task mail, which insert the starting row, the pending
 * task and the starter's task monitor. */
export async function start(
  req: Request,
  args: { readonly agent: string; readonly task: string },
  plan: StartPlan,
): Promise<Started | Refused> {
  const refused = await startChecks(req, args.agent, plan);
  if (refused !== undefined) return req.refuse(refused);
  const listed = plan.agents.get(args.agent);
  if (listed === undefined) throw new Error("startChecks lists the agent");
  const team = req.team.team_id;
  const k =
    1 +
    (await memberRows(req.tx, team)).filter(
      (r) => r.role === "member" && r.agent === args.agent,
    ).length;
  const member = {
    tenant: req.team.tenant_id,
    team,
    name: MemberName.parse(`${args.agent}-${k}`),
    generation: 1,
  };
  const startedId = req.batch.nextId();
  req.batch.add({
    type: "member_started",
    type_version: 1,
    critical: true,
    actor: { kind: "host" },
    data: {
      member,
      agent: args.agent,
      config_hash: listed.configHash,
      thread_id: plan.threadId,
      parent: await req.parent(startedId),
      provenance: req.provenance,
      ...(listed.budget === undefined ? {} : { budget: listed.budget }),
      ...(plan.resolved.ok ? plan.resolved.value : {}),
    },
  });
  req.batch.add(
    sent({
      mail_id: req.mailId,
      kind: "task",
      team,
      from: req.from,
      to: { name: member.name, generation: 1 },
      provenance: req.provenance,
      causal: req.causal,
      body: await bodyOf(args.task, req.put),
    }),
  );
  return req.done({ member, status: "started" } satisfies Started);
}

/**
 * Policy (a lead, or the operator of the team's tenant, starts), team open, the agent listed, the
 * start's chosen fields, the concurrent cap, headroom.
 */
async function startChecks(
  req: Request,
  agent: string,
  plan: StartPlan,
): Promise<Refusal | undefined> {
  const denied = req.decide("start", agent);
  if (denied !== undefined) return denied;
  if (req.team.closed_at !== null) return refusal("team_closed");
  if (!plan.agents.has(agent)) return refusal("unknown_agent");
  if (!plan.resolved.ok)
    return refusal("invalid_definition", plan.resolved.error);
  const live = (await memberRows(req.tx, req.team.team_id)).filter(
    (r) => r.role === "member" && LIVE.has(r.state),
  );
  if (live.length >= plan.limits.concurrent) return refusal("concurrency_cap");
  return (await plan.headroom(agent, req.tx))
    ? undefined
    : refusal("budget_exceeded");
}

/** mail.send: a message to a member, pending until its writer consumes it. */
export async function send(
  req: Request,
  to: Target,
  text: string,
  limits: TeamLimits,
): Promise<Sent | Refused> {
  const row = await deliverable(req, "send", to, limits);
  if ("refused" in row) return req.refuse(row);
  req.batch.add(
    sent({
      mail_id: req.mailId,
      kind: "message",
      team: req.team.team_id,
      from: req.from,
      to: { name: row.name, generation: row.generation },
      provenance: req.provenance,
      causal: req.causal,
      body: await bodyOf(text, req.put),
    }),
  );
  return req.done({ id: req.mailId, status: "sent" } satisfies Sent);
}

/** send and ask, after the policy: team open, the member known at its generation, not ended, not
 * the sender, and its mailbox not full. */
export async function deliverable(
  req: Request,
  op: "send" | "ask",
  to: Target,
  limits: TeamLimits,
): Promise<MemberRow | Refusal> {
  const denied = req.decide(op, to.name);
  if (denied !== undefined) return denied;
  if (req.team.closed_at !== null) return refusal("team_closed");
  const row = await to.row();
  if ("refused" in row) return row;
  if (row.state === "ended") return refusal("member_ended");
  if (row.name === req.self?.name) return refusal("self");
  const pending = (await pendingTo(req.tx, row.team_id, row.name)).length;
  return pending >= limits.mailbox ? refusal("mailbox_full") : row;
}
