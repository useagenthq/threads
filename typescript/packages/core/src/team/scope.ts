import type { BranchId, TeamId, ThreadId } from "../log";
import type { SqlValue } from "../store/driver";

/** The log an append belongs to. */
export type TeamLog = {
  readonly threadId: ThreadId;
  readonly branchId: BranchId;
};

/** Limits every row write to one team: a rebuild must not touch a nested team's rows. */
export type Scope = TeamId | undefined;

export const inScope = (scope: Scope, team: string): boolean =>
  scope === undefined || scope === team;

/** `AND` clause and params that keep an update or delete inside the scope. */
export const SCOPED = "AND (CAST(? AS TEXT) IS NULL OR team_id = ?)";
export const scoped = (scope: Scope): readonly SqlValue[] => [
  scope ?? null,
  scope ?? null,
];
