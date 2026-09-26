import type { ErrorCode } from "../log";

/**
 * Codes the store returns that the log never records: a subset of `ApiErrorCode` in
 * spec/schema/api.schema.json (a test pins it there).
 */
export type StoreApiCode =
  | "branch_not_found"
  | "branch_exists"
  | "not_found"
  | "invalid_request"
  | "sandbox_required"
  | "busy"
  | "thread_in_team"
  | "path_exists"
  | "bundle_incomplete"
  | "schedule_conflict"
  | "io_error";

/** A typed log failure. Runners compare `code` and `seq`, never `message`. */
export type LogError = {
  readonly code: ErrorCode | StoreApiCode;
  readonly message: string;
  readonly seq?: number;
};

export function logError(
  code: ErrorCode | StoreApiCode,
  message: string,
  seq?: number,
): LogError {
  return seq === undefined ? { code, message } : { code, message, seq };
}
