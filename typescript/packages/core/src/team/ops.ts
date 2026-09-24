import type { z } from "zod";
import type { Budget, ThreadId } from "../log";
import type { Result } from "../result";
import {
  addressed,
  type CallContext,
  type Caller,
  callerOf,
  callMailId,
  causalOf,
  decide,
  type Refusal,
  recorded,
  refusal,
} from "./call";
import type { InvalidDefinition, Resolved } from "./dynamic";
import { bodyOf, sent } from "./mail";
import { memberRows, pendingTo } from "./rows";

// The model tools start and send (spec/schema/README.md, "Teams"; design §4.5 and §4.10), each
// decided inside the caller's append: the policy decision, then the op's checks in the order the
// op vectors pin, then its events and the call's one result. Reference:
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
  readonly headroom: (agent: string) => boolean;
  /** The new member's thread id, minted before the append. */
  readonly threadId: ThreadId;
};

const LIVE: ReadonlySet<string> = new Set(["starting", "running"]);

/** member.start: member_started and its task mail, which insert the starting row, the pending
 * task and the starter's task monitor. */
export function start(
  ctx: CallContext,
  args: { readonly agent: string; readonly task: string },
  plan: StartPlan,
): void {
  const caller = callerOf(ctx);
  if (caller === undefined) throw new Error("a team tool call outside a team");
  const refused = startChecks(ctx, caller, args.agent, plan);
  if (refused !== undefined) {
    recorded(ctx, refused);
    return;
  }
  const listed = plan.agents.get(args.agent);
  if (listed === undefined) throw new Error("startChecks lists the agent");
  const team = caller.team.team_id;
  const k =
    1 +
    memberRows(ctx.db, team).filter(
      (r) => r.role === "member" && r.agent === args.agent,
    ).length;
  const member = {
    tenant: caller.team.tenant_id,
    team,
    name: `${args.agent}-${k}`,
    generation: 1,
  };
  const startedId = ctx.batch.nextId();
  ctx.batch.add({
    type: "member_started",
    type_version: 1,
    critical: true,
    actor: { kind: "host" },
    data: {
      member,
      agent: args.agent,
      config_hash: listed.configHash,
      thread_id: plan.threadId,
      parent: {
        thread_id: ctx.call.thread_id,
        branch_id: ctx.call.branch_id,
        event_id: startedId,
        relation: "team_member",
      },
      provenance: caller.provenance,
      ...(listed.budget === undefined ? {} : { budget: listed.budget }),
      ...(plan.resolved.ok ? plan.resolved.value : {}),
    },
  });
  ctx.batch.add(
    sent({
      mail_id: callMailId(ctx),
      kind: "task",
      team,
      from: caller.ref,
      to: { name: member.name, generation: 1 },
      provenance: caller.provenance,
      causal: causalOf(ctx),
      body: { text: args.task },
    }),
  );
  recorded(ctx, { member, status: "started" });
}

/**
 * Policy (only a lead starts), team open, the agent listed, the start's chosen fields, the
 * concurrent cap, headroom.
 */
function startChecks(
  ctx: CallContext,
  caller: Caller,
  agent: string,
  plan: StartPlan,
): Refusal | undefined {
  const denied = decide(ctx, "start", agent, caller.row.role === "lead");
  if (denied !== undefined) return denied;
  if (caller.team.closed_at !== null) return refusal("team_closed");
  if (!plan.agents.has(agent)) return refusal("unknown_agent");
  if (!plan.resolved.ok)
    return refusal("invalid_definition", plan.resolved.error);
  const live = memberRows(ctx.db, caller.team.team_id).filter(
    (r) => r.role === "member" && LIVE.has(r.state),
  );
  if (live.length >= plan.limits.concurrent) return refusal("concurrency_cap");
  return plan.headroom(agent) ? undefined : refusal("budget_exceeded");
}

/** mail.send: a message to a member, pending until its writer consumes it. */
export function send(
  ctx: CallContext,
  args: { readonly to: string; readonly text: string },
  limits: TeamLimits,
): void {
  const caller = callerOf(ctx);
  if (caller === undefined) throw new Error("a team tool call outside a team");
  const row = deliverable(ctx, caller, args.to, limits);
  if ("refused" in row) {
    recorded(ctx, row);
    return;
  }
  const id = callMailId(ctx);
  ctx.batch.add(
    sent({
      mail_id: id,
      kind: "message",
      team: caller.team.team_id,
      from: caller.ref,
      to: { name: row.name, generation: row.generation },
      provenance: caller.provenance,
      causal: causalOf(ctx),
      body: bodyOf(args.text, ctx.put),
    }),
  );
  recorded(ctx, { id, status: "sent" });
}

/** After the policy: team open, the member known at its generation, not ended, not the sender,
 * and its mailbox not full. */
function deliverable(
  ctx: CallContext,
  caller: Caller,
  to: string,
  limits: TeamLimits,
): ReturnType<typeof addressed> {
  const denied = decide(ctx, "send", to, true);
  if (denied !== undefined) return denied;
  if (caller.team.closed_at !== null) return refusal("team_closed");
  const row = addressed(ctx, caller, to);
  if ("refused" in row) return row;
  if (row.state === "ended") return refusal("member_ended");
  if (row.name === caller.row.name) return refusal("self");
  const pending = pendingTo(ctx.db, row.team_id, row.name).length;
  return pending >= limits.mailbox ? refusal("mailbox_full") : row;
}
