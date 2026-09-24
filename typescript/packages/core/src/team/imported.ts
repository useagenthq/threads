import type { TeamId } from "../log";
import { ok, type Result } from "../result";
import type { LogStore } from "../store";
import { knownOf } from "../store/indexing";
import { openedThreads } from "../store/started";
import { refoldWakes } from "../store/wakes";
import type { VerifiedLog } from "../verify";
import type { LogError } from "../verify/error";
import { rebuildTeamIndex } from "./rebuild";

// An import stores a log's bytes without its appends' hooks, so in the same transaction the
// index is folded again from the logs: each imported branch's wake rows, and every team the log
// belongs to, rebuilt whole. A team whose lead is not stored yet has no rows until its lead is
// imported (that import rebuilds it). A log that breaks rule 43 with the stored team is refused.

/** Folds the index rows an imported `log` changes; runs inside the import's transaction. */
export function indexImported(
  store: LogStore,
  log: VerifiedLog,
): Result<void, LogError> {
  for (const s of log.segments)
    refoldWakes(store.driver, s.header.branch_id, knownOf(s.events));
  const teams = teamsOf(store, log);
  if (!teams.ok) return teams;
  for (const team of teams.value) {
    const rebuilt = rebuildTeamIndex(store, team);
    if (!rebuilt.ok && rebuilt.error.code !== "not_found") return rebuilt;
  }
  return ok(undefined);
}

/**
 * The teams the log's thread belongs to, from its opening event: the team it leads, the team
 * whose log it is, and, for a member, its lead's team.
 */
function teamsOf(
  store: LogStore,
  log: VerifiedLog,
): Result<readonly TeamId[], LogError> {
  const first = log.segments[0]?.events[0];
  const e = first?.kind === "event" ? first.event : undefined;
  if (e?.type === "team_opened") return ok([e.data.team]);
  if (e?.type !== "thread_started") return ok([]);
  const own = e.data.team === undefined ? [] : [e.data.team.id];
  const parent = e.data.parent;
  if (parent?.relation !== "team_member") return ok(own);
  const opened = openedThreads(store.driver, store.tenant);
  if (!opened.ok) return opened;
  const lead = opened.value.find((o) => o.threadId === parent.thread_id);
  const team =
    lead?.event.type === "thread_started"
      ? lead.event.data.team?.id
      : undefined;
  return ok(team === undefined ? own : [...own, team]);
}
