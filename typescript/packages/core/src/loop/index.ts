import type { ArtifactStore, Writer } from "../store";
import { recover } from "./recover";
import { type LoopEnd, runLoop } from "./run";
import { Session } from "./session";
import type { LoopConfig } from "./types";

export { FINAL_OUTPUT } from "./output";
export { RETRY_DEFAULTS } from "./policy";
export type { LoopEnd } from "./run";
export { Session } from "./session";
export { type RecordedStubs, recordedStubs } from "./stubs";
export type {
  Authorization,
  Clock,
  Halt,
  LoopConfig,
  RunErrorCode,
  StubGateway,
  ToolContext,
  ToolImpl,
  ToolRun,
} from "./types";

/**
 * Runs a branch under a freshly acquired lease: recovery first, then the
 * loop until the branch is idle or parked. `runLoop` alone skips recovery; a new lease never
 * does.
 */
export async function resume(
  writer: Writer,
  artifacts: ArtifactStore,
  config: LoopConfig,
  options: { readonly loop: boolean } = { loop: true },
): Promise<LoopEnd> {
  const s = new Session(writer, artifacts, config);
  const stopped = await recover(s);
  if (stopped !== undefined) return { kind: "halted", halt: stopped };
  if (!options.loop)
    return { kind: s.fold.parked.length > 0 ? "parked" : "idle" };
  return runLoop(s);
}
