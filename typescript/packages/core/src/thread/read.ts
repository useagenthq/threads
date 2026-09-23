import type { BranchId } from "../log";
import { err, type Result } from "../result";
import type { LogStore } from "../store";
import { type LogError, logError, type VerifiedLog } from "../verify";

/** A branch's log as a reader sees it; a failure other than an unsupported line is log_corrupt. */
export function readLog(
  log: LogStore,
  branchId: BranchId,
): Result<VerifiedLog, LogError> {
  const read = log.read(branchId);
  if (read.ok) return read;
  const { code, message, seq } = read.error;
  return code === "unsupported_format" || code === "unsupported_critical_event"
    ? read
    : err(logError("log_corrupt", message, seq));
}
