import type { BranchId } from "../log";
import { err, type Result } from "../result";
import type { LogStore } from "../store";
import type { LogError, VerifiedLog } from "../verify";

/** The read errors of spec/api.json: why a stored log can't be read. */
export type ReadErrorCode =
  | "log_corrupt"
  | "unsupported_format"
  | "unsupported_critical_event";

export type ReadError = LogError & { readonly code: ReadErrorCode };

export function readError(
  code: ReadErrorCode,
  message: string,
  seq?: number,
): ReadError {
  return seq === undefined ? { code, message } : { code, message, seq };
}

/** A branch's log as a reader sees it; a failure other than an unsupported line is log_corrupt. */
export async function readLog(
  log: LogStore,
  branchId: BranchId,
): Promise<Result<VerifiedLog, ReadError>> {
  const read = await log.read(branchId);
  if (read.ok) return read;
  const { code, message, seq } = read.error;
  const unsupported =
    code === "unsupported_format" || code === "unsupported_critical_event";
  return err(readError(unsupported ? code : "log_corrupt", message, seq));
}
