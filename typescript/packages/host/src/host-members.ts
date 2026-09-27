import {
  ensureHostTeam,
  err,
  type HostMemberStart,
  type HostPin,
  type HostTeamIds,
  hostTeam,
  hostTeamIds,
  LOCAL_TENANT,
  type MemberEntry,
  memberRows,
  type OnTeamLog,
  ok,
  onTeamLog,
  type Principal,
  pinHostMember,
  type Result,
  reading,
  storeConnection,
  type Team,
  ThreadId,
  uuidv7,
} from "threadsai/host";
import { z } from "zod";
import type { HostContext } from "./context";
import type { HostMember } from "./members";

// The host side of lane 29D: the lazy open of a tenant's host team, and Host.team. The ids are
// derived, so the open is idempotent across processes: a loser collides on the branch's primary
// key and gets already_open, which is success.

/**
 * What each configured host member is rebound and run with: its runner's policy-aware member
 * entry, so only a messagePolicy rule can allow a host member's own send or ask (a host team has
 * no grant of its own).
 */
function memberEntriesOf(
  ctx: HostContext,
  members: readonly HostMember[],
): ReadonlyMap<string, MemberEntry> {
  return new Map(
    [...ctx.agents.values()].flatMap((a) =>
      members.some((m) => m.agent === a.name)
        ? [[a.name, a.runner.member] as const]
        : [],
    ),
  );
}

/**
 * Each configured host member's pin as this host holds it now: what the supervisor's restart and
 * an operator's start the next generation on, so a definition that was changed or re-registered
 * is picked up by the restart rather than repeated.
 */
export function hostPinOf(
  ctx: HostContext,
  members: readonly HostMember[],
  tenant: string,
): HostPin {
  return async (name) => {
    const hosted = [...ctx.agents.values()].find((a) => a.name === name);
    if (hosted === undefined || !members.some((m) => m.agent === name))
      return undefined;
    const { artifacts } = await ctx.open(tenant);
    return await pinHostMember(artifacts, hosted.runner.member);
  };
}

/** The team limits a leadless host team's sends and asks are capped by (a lead's defaults). */
const HOST_TEAM_LIMITS = { concurrent: 64, mailbox: 100 } as const;

/**
 * Opens the tenant's host team if it is not open, and starts every configured member that has no
 * row yet. Safe to call on every tick and from a caller's own send.
 */
export async function openHostTeam(
  ctx: HostContext,
  members: readonly HostMember[],
  tenant: string,
): Promise<Result<HostTeamIds, { readonly message: string }>> {
  const ids = hostTeamIds(tenant);
  // One indexed read first: the open is idempotent, but pinning the members and taking the team
  // log's lease to append nothing is not, and every tick and every run of the tenant asks.
  if (await started(ctx, members, ids)) return ok(ids);
  const { log, artifacts } = await ctx.open(tenant);
  const starts: HostMemberStart[] = [];
  for (const member of members) {
    const hosted = [...ctx.agents.values()].find(
      (a) => a.name === member.agent,
    );
    if (hosted === undefined) continue;
    // Pinned as a member, under the host's rules: a host member's team tools are reply plus what
    // its rules allow, and it inherits no lead's defer_tools (it has no lead). Its canonical bytes
    // and its deferred tools' specs are stored before the append that names its config_hash.
    starts.push({
      agent: member.agent,
      configHash: await pinHostMember(artifacts, hosted.runner.member),
      threadId: ThreadId.parse(uuidv7(log.now())),
    });
  }
  const opened = await ensureHostTeam(
    log,
    tenant,
    starts,
    log.now(),
    `host-members-${crypto.randomUUID()}`,
    (ids, decide) => appendTo(ctx, tenant, ids, decide),
  );
  return opened.ok
    ? opened
    : err({ message: `${opened.error.code}: ${opened.error.message}` });
}

/** The tenant's host team is open and every configured member already has its row. */
async function started(
  ctx: HostContext,
  members: readonly HostMember[],
  ids: HostTeamIds,
): Promise<boolean> {
  const { db } = await storeConnection(ctx.store);
  const rows = await reading(db, (tx) => memberRows(tx, ids.teamId));
  const known = new Set(rows.map((r) => String(r.name)));
  return rows.length > 0 && members.every((m) => known.has(m.agent));
}

/** One append on an already-open host team log, under its writer. */
const appendTo: (
  ctx: HostContext,
  tenant: string,
  ids: HostTeamIds,
  decide: Parameters<OnTeamLog>[1],
) => ReturnType<OnTeamLog> = async (ctx, tenant, ids, decide) => {
  const { log } = await ctx.open(tenant);
  await onTeamLog(log, ids.branchId, async (tx, batch) => {
    for (const draft of await decide(tx.tx)) batch.add(draft);
  });
  return ok(undefined);
};

/**
 * The host team of every tenant this store holds, and of the local tenant: called from ready() and
 * from the team tick, so a configured member exists before anything addresses it. A tenant whose
 * first thread is created later gets its host team from that caller's own send.
 *
 * ponytail: one already_open attempt per tenant per tick, since the open is a single indexed read.
 */
export async function openHostTeams(
  ctx: HostContext,
  members: readonly HostMember[],
): Promise<void> {
  if (members.length === 0) return;
  const { db } = await storeConnection(ctx.store);
  const rows = await reading(db, (tx) =>
    tx.all("SELECT DISTINCT tenant_id FROM threads"),
  );
  const parsed = z
    .array(z.strictObject({ tenant_id: z.string() }))
    .safeParse(rows);
  const tenants = new Set([
    LOCAL_TENANT,
    ...(parsed.success ? parsed.data.map((r) => r.tenant_id) : []),
  ]);
  for (const tenant of tenants) await openHostTeam(ctx, members, tenant);
}

/** Host.team({principal}): a handle on the principal's tenant's host team. */
export async function hostTeamOf(
  ctx: HostContext,
  members: readonly HostMember[],
  principal: Principal,
): Promise<
  Result<Team, { readonly code: "not_found"; readonly message: string }>
> {
  // Lazy: the first Host.team of a tenant opens its host team, as a caller's first send does.
  if (members.length > 0) await openHostTeam(ctx, members, principal.tenant);
  return await hostTeam(ctx.storeFor(principal.tenant), principal, {
    limits: HOST_TEAM_LIMITS,
    agents: memberEntriesOf(ctx, members),
  });
}
