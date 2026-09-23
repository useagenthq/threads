import type { BranchId } from "../log";
import { knownEvents } from "../reduce";
import { refReader, verifyRequests } from "../render";
import { err, type Result } from "../result";
import type { LogStore } from "../store";
import type { ArtifactStore } from "../store/artifacts";
import type { LogError } from "../verify";
import { type ReadErrorCode, readLog } from "./read";

// Thread.replay() (spec/api.json): the import-time request check, re-run over the branch with
// the code running now. No model, tool or sandbox call, no lease and no append.

/** Why a recorded request no longer reproduces, or why the branch can't be read. */
export type ReplayError = LogError & {
  readonly code:
    | ReadErrorCode
    | "prefix_changed"
    | "request_hash_mismatch"
    | "artifact_missing"
    | "artifact_corrupt";
};

export function replay(
  log: LogStore,
  artifacts: Pick<ArtifactStore, "get">,
  branchId: BranchId,
): Result<void, ReplayError> {
  const read = readLog(log, branchId);
  if (!read.ok) return read;
  const checked = verifyRequests(knownEvents(read.value), refReader(artifacts));
  if (checked.ok) return checked;
  const { code, message, seq } = checked.error;
  const at = seq === undefined ? {} : { seq };
  switch (code) {
    case "prefix_changed":
    case "request_hash_mismatch":
    case "artifact_missing":
    case "artifact_corrupt":
      return err({ code, message, ...at });
    default:
      // The verifier fails only with the four codes above; anything else is a corrupt log.
      return err({ code: "log_corrupt", message, ...at });
  }
}
