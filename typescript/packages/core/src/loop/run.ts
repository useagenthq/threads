import { assertNever } from "../assert-never";
import type { EventOf } from "../fold/state";
import { settleBackground } from "./agents/background";
import { parkOn } from "./agents/park";
import { finish as record, runChild, spawnedFor } from "./agents/spawn";
import { closeUnrecorded } from "./calls";
import { draft } from "./drafts";
import { afterStep, finish } from "./lifecycle";
import { runCalls } from "./parallel";
import { requestTurn } from "./request";
import { respond } from "./respond";
import type { Session } from "./session";
import { nextStep } from "./turn";
import type { Halt } from "./types";

// The loop: every step is decided from the committed log (turn.ts), so a fresh run, a resumed
// one and a recovered one take the same path. It runs until the branch is idle or parked.

export type LoopEnd =
  | { readonly kind: "idle" }
  | { readonly kind: "parked" }
  | { readonly kind: "halted"; readonly halt: Halt };

export async function runLoop(s: Session): Promise<LoopEnd> {
  for (;;) {
    const settled = await settleBackground(s);
    if (settled !== undefined) return { kind: "halted", halt: settled };
    const seq = s.fold.seq;
    const step = nextStep(s.events, s.fold);
    let stopped: Halt | undefined;
    switch (step.kind) {
      case "idle":
      case "parked":
        return step;
      case "cancel":
        stopped = await cancel(s, step.request);
        break;
      case "calls":
        stopped = await runCalls(s);
        break;
      case "request":
        stopped = await requestTurn(s);
        break;
      case "respond":
        stopped = await respond(s, step.response);
        break;
      case "end_turn":
        stopped = await finish(s);
        break;
      default:
        return assertNever(step);
    }
    stopped ??= await afterStep(s, seq);
    if (stopped !== undefined) return { kind: "halted", halt: stopped };
    // Every step appends; one that doesn't would spin forever, which is a bug.
    if (s.fold.seq === seq)
      throw new Error(`loop step ${step.kind} made no progress`);
  }
}

/**
 * calls that never began close as not_executed; cancelled is appended only
 * once no effect is begun or unknown (those park instead, through recovery). A running child is
 * cancelled and its end recorded first; one that parks parks this thread (F7.6).
 */
/** A running child under its parent's cancel: its end recorded, or what stops this run. */
async function cancelChild(
  s: Session,
  spawned: EventOf<"agent_spawned">,
  principal: EventOf<"cancel_requested">["actor"]["principal"],
): Promise<Halt | undefined | "recorded"> {
  const end = await runChild(s, spawned, principal);
  if ("code" in end) return end;
  if (end.status === "parked") return parkOn(s, spawned, end.reason);
  return record(s, spawned, end, false) ?? "recorded";
}

async function cancel(
  s: Session,
  request: EventOf<"cancel_requested">,
): Promise<Halt | undefined> {
  for (const callId of [...s.fold.pending]) {
    const spawned = spawnedFor(s, callId);
    if (
      spawned !== undefined &&
      s.fold.children.get(spawned.data.child_thread_id) === "running"
    ) {
      const done = await cancelChild(s, spawned, request.actor.principal);
      if (done !== "recorded") return done;
      continue;
    }
    const stopped = s.append(
      draft.toolResult(
        {
          call_id: callId,
          is_error: true,
          origin: "not_executed",
          preview: "not executed: cancelled",
        },
        { kind: "host" },
      ),
    );
    if (stopped !== undefined) return stopped;
  }
  const closed = closeUnrecorded(s);
  if (closed !== undefined) return closed;
  // One batch: a lease lost between them can't leave the cancellation half recorded.
  return s.append(
    draft.cancelled({ request_event_id: request.event_id }),
    draft.turnCompleted("cancelled"),
  );
}
