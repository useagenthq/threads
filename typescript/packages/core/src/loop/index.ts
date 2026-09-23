import type { ArtifactStore, EventDraft, Writer } from "../store";
import { drainBackground, resumeBackground } from "./agents/background";
import { observe } from "./hooks";
import { sessionStart } from "./lifecycle";
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
  Agents,
  Authorization,
  ChildEnd,
  ChildRun,
  Clock,
  Covering,
  Halt,
  LoopConfig,
  RunErrorCode,
  StubGateway,
  Subagent,
  Team,
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
  const end = await session(s, options.input);
  // session_end observes; its failure is recorded and changes nothing.
  if (end.kind !== "halted") await observe(s, "session_end", []);
  return end;
}

async function session(s: Session, input?: EventDraft): Promise<LoopEnd> {
  const source = s.events.some((e) => e.type === "user_input")
    ? "resume"
    : "startup";
  const denied = await sessionStart(s, source);
  if (denied !== undefined) return { kind: "halted", halt: denied };
  resumeBackground(s);
  const end = await turns(s, input);
  if (end.kind === "halted") return end;
  // Background children end while this writer holds the lease; their results are recorded.
  const drained = await drainBackground(s);
  return drained === undefined ? end : { kind: "halted", halt: drained };
}

async function turns(s: Session, input?: EventDraft): Promise<LoopEnd> {
  const earlier = await runLoop(s);
  if (earlier.kind !== "idle" || input === undefined) return earlier;
  const appended = s.append(input);
  return appended === undefined
    ? runLoop(s)
    : { kind: "halted", halt: appended };
}
