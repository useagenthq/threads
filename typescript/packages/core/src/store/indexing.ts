import type { BranchId, KnownEvent, ThreadId } from "../log";
import { ok, type Result } from "../result";
import { feedRows, indexRows, openTeamLog } from "../team/write";
import type { ChainEvent } from "../verify";
import type { LogError } from "../verify/error";
import type { SqliteDriver } from "./driver";

/** One append as the index hooks see it, inside its transaction, after its event rows. */
export type Appended = {
  readonly db: SqliteDriver;
  readonly tenant: string;
  readonly threadId: ThreadId;
  readonly branchId: BranchId;
  readonly events: readonly KnownEvent[];
  /** The ids of the appended events that opened a turn. */
  readonly opened: ReadonlySet<string>;
  /** The appending lease's holder, and the append's clock. */
  readonly holderId: string;
  readonly now: number;
};

/**
 * Writes index rows from an append's events (the replay rule). An error rolls the whole append
 * back, as a writer's `alongside` does.
 */
export type IndexHook = (append: Appended) => Result<void, LogError>;

/**
 * Every index hook, in the order each append runs them: the rows its events insert and change
 * (pending_wakes included), then a lead's first append opens its team log, then the feed, so a
 * new team's feed starts with team_opened.
 */
const INDEX_HOOKS: readonly IndexHook[] = [indexRows, openTeamLog, feedRows];

/** The known events among appended lines: an unknown non-critical event writes no row. */
export function knownOf(lines: readonly ChainEvent[]): readonly KnownEvent[] {
  return lines.flatMap((line) => (line.kind === "event" ? [line.event] : []));
}

/** Runs every index hook over one append; the first error wins. */
export function indexAppend(append: Appended): Result<void, LogError> {
  for (const hook of INDEX_HOOKS) {
    const done = hook(append);
    if (!done.ok) return done;
  }
  return ok(undefined);
}
