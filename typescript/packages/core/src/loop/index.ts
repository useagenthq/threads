import type { ArtifactStore, EventDraft, Writer } from "../store";
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
 * Runs a branch under a freshly acquired lease: recovery first, then, when
 * the branch is idle, the new `input` (a user_input draft), then the loop until the branch is
 * idle or parked. A branch left parked by recovery takes no new input.
 */
export async function resume(
  writer: Writer,
  artifacts: ArtifactStore,
  config: LoopConfig,
  options: { readonly loop?: boolean; readonly input?: EventDraft } = {},
): Promise<LoopEnd> {
  const s = new Session(writer, artifacts, config);
  const stopped = await recover(s);
  if (stopped !== undefined) return { kind: "halted", halt: stopped };
  if (options.loop === false)
    return { kind: s.fold.parked.length > 0 ? "parked" : "idle" };
  const earlier = await runLoop(s);
  if (earlier.kind !== "idle" || options.input === undefined) return earlier;
  const appended = s.append(options.input);
  return appended === undefined
    ? runLoop(s)
    : { kind: "halted", halt: appended };
}
