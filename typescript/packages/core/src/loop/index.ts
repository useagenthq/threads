import { ConfigError } from "../agent/errors";
import { isTestKit } from "../model/guard";
import { endedOtherwise } from "../reduce/run-end";
import type { ArtifactStore, EventDraft, Writer } from "../store";
import { resumeBackground } from "./agents/background";
import { teamTurns } from "./agents/members";
import { unparkChildren } from "./agents/park";
import { runStatus, stopChildren } from "./agents/stop";
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
  ChildDone,
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
  TeamAgentPin,
  TeamRuntime,
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
  refuseHostedStub(s);
  const stopped = await recover(s);
  if (stopped !== undefined) return { kind: "halted", halt: stopped };
  if (options.loop === false)
    return { kind: s.fold.parked.length > 0 ? "parked" : "idle" };
  const end = await session(s, options.input);
  // session_end observes; its failure is recorded and changes nothing.
  if (end.kind !== "halted") await observe(s, "session_end", []);
  return end;
}

/**
 * Hosted calls run inside the provider and can't be stubbed, so a stub-mode run with a live
 * model declaring any is refused before recovery or dispatch.
 */
function refuseHostedStub(s: Session): void {
  if (s.config.stub === undefined || s.fold.model === undefined) return;
  const model = s.config.models(s.fold.model);
  if (
    model !== undefined &&
    !isTestKit(model) &&
    (model.info.hosted_tools ?? []).length > 0
  )
    throw new ConfigError(
      "hosted_tool_unsupported",
      `stub mode can't stub the hosted tools of live model ${model.info.model.provider}/${model.info.model.name}`,
    );
}

async function session(s: Session, input?: EventDraft): Promise<LoopEnd> {
  const source = s.events.some((e) => e.type === "user_input")
    ? "resume"
    : "startup";
  const denied = await sessionStart(s, source);
  if (denied !== undefined) return { kind: "halted", halt: denied };
  const unparked = await unparkChildren(s);
  if (unparked !== undefined) return { kind: "halted", halt: unparked };
  resumeBackground(s);
  if (s.config.team === undefined)
    return waitForChildren(s, await turns(s, input));
  const turn = async (): Promise<LoopEnd> =>
    waitForChildren(s, await runLoop(s));
  // A lead parked on its members first waits for them, as a parent runs the children it is
  // parked on: their settlements resume it, and only then does a new input start a turn.
  const { parked } = s.fold;
  if (parked.length > 0 && parked.every((p) => p.kind === "member")) {
    const unparked = await teamTurns(s, { kind: "parked" }, turn);
    if (unparked.kind !== "idle") return unparked;
  }
  return teamTurns(s, await waitForChildren(s, await turns(s, input)), turn);
}

/**
 * A run waits for its background children while this writer holds the lease: each end is
 * recorded, and one recorded while no turn is open wakes this thread for another turn
 * (spec/schema/README.md, "Background wakes" and "Run completion"). A cancelled run returns
 * once its own log records the cancel; one that ended failed, budget_exhausted or handed_off
 * first stops its own children and records their ends. A parked branch stops once nothing more
 * is running; an end held for another run stays for the next run.
 */
async function waitForChildren(s: Session, first: LoopEnd): Promise<LoopEnd> {
  let end = first;
  while (end.kind !== "halted") {
    const status = runStatus(s);
    if (status === "cancelled") {
      // Its children have durable barriers already; a later run or a host records their ends.
      void Promise.allSettled(s.background.values());
      return end;
    }
    if (endedOtherwise(status)) return stopChildren(s, end);
    const idleWithEnds = end.kind === "idle" && s.finished.size > 0;
    if (!idleWithEnds && s.background.size === 0) return end;
    // The lead's own log moving (a cancel) wakes the wait too, even while a child hangs.
    if (!idleWithEnds)
      await Promise.race([...s.background.values(), s.moved()]);
    end = await runLoop(s);
  }
  return end;
}

async function turns(s: Session, input?: EventDraft): Promise<LoopEnd> {
  const earlier = await runLoop(s);
  if (earlier.kind !== "idle" || input === undefined) return earlier;
  const appended = s.append(input);
  return appended === undefined
    ? runLoop(s)
    : { kind: "halted", halt: appended };
}
