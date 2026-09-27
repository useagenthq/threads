import { MemberName, type ThreadId } from "../log";
import { ok, type Result } from "../result";
import type { LogStore } from "../store";
import type { EventDraft } from "../store/admit";
import type { Tx } from "../store/driver";
import { ALREADY_OPEN, openBranch } from "../store/open";
import type { LogError } from "../verify/error";
import { type HostTeamIds, hostTeamIds } from "./host-team";
import { memberRows } from "./rows";

// The lazy, idempotent open of a tenant's host team (spec/schema/README.md, "Teams Phase 2"). The
// ids are derived, so two processes racing the open collide on the branch's primary key and the
// loser gets already_open: that is the required behaviour, not an error. Its log holds
// team_opened{kind: host} and one member_started{host_member} per configured host member; it
// never closes.

/** One configured host member, pinned before the open: its agent is also its member name. */
export type HostMemberStart = {
  readonly agent: string;
  /** The pinned definition's config_hash, whose canonical bytes are stored before the append. */
  readonly configHash: string;
  /** The member's root thread, minted before the append and opened at materialize. */
  readonly threadId: ThreadId;
};

function opened(tenant: string, ids: HostTeamIds): EventDraft {
  return {
    type: "team_opened",
    type_version: 1,
    critical: true,
    actor: { kind: "host" },
    data: { team: ids.teamId, kind: "host", tenant },
  };
}

/**
 * A host member's start: no parent (its thread is a root), no provenance, no define, no label, no
 * budget, no task mail and no task monitor. Generation 1 is the first; a restart is lane 29E's.
 */
function started(
  tenant: string,
  ids: HostTeamIds,
  member: HostMemberStart,
): EventDraft {
  return {
    type: "member_started",
    type_version: 1,
    critical: true,
    actor: { kind: "host" },
    data: {
      member: {
        tenant,
        team: ids.teamId,
        name: MemberName.parse(member.agent),
        generation: 1,
      },
      agent: member.agent,
      config_hash: member.configHash,
      thread_id: member.threadId,
      host_member: true,
    },
  };
}

/**
 * Opens the tenant's host team if it is not open, then starts every configured member that has no
 * row yet. Idempotent: the open tolerates already_open, and the follow-up append re-reads the rows
 * inside its own transaction, so two processes never insert one name twice.
 */
export async function ensureHostTeam(
  log: LogStore,
  tenant: string,
  members: readonly HostMemberStart[],
  now: number,
  holderId: string,
  append: OnTeamLog,
): Promise<Result<HostTeamIds, LogError>> {
  const ids = hostTeamIds(tenant);
  const open = await openBranch(log.driver, now, {
    tenantId: tenant,
    threadId: ids.threadId,
    branchId: ids.branchId,
    // Free at once: the next writer (the host's team tick, a caller's send) takes epoch 2.
    lease: { holderId, ttlMs: 0 },
    drafts: [
      opened(tenant, ids),
      ...members.map((m) => started(tenant, ids, m)),
    ],
  });
  if (!open.ok) return open;
  if (open.value !== ALREADY_OPEN) return ok(ids);
  if (members.length === 0) return ok(ids);
  const late = await append(ids, async (tx) => {
    const rows = await memberRows(tx, ids.teamId);
    const known = new Set<string>(rows.map((r) => r.name));
    return members
      .filter((m) => !known.has(m.agent))
      .map((m) => started(tenant, ids, m));
  });
  return late.ok ? ok(ids) : late;
}

/**
 * How `ensureHostTeam` appends to an already-open host team log: under its writer, with the drafts
 * the callback decides inside that append's own transaction. The host wires its team-log writer
 * (`onTeamLog`) here; a held lease is the caller's to retry.
 */
export type OnTeamLog = (
  ids: HostTeamIds,
  decide: (tx: Tx) => Promise<readonly EventDraft[]>,
) => Promise<Result<void, LogError>>;
