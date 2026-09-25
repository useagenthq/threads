import type { EventOf } from "../../fold/state";
import type { Outcome } from "../../hooks/invoke";
import { decision, defining, recorded, run } from "../hooks";
import type { Session } from "../session";
import { turnEvents } from "../turn";
import type { Halt } from "../types";

// subagent_start and subagent_stop: each decision is recorded against the
// spawn call before the work it gates, and read back instead of re-running the hook.

type Call = EventOf<"tool_call">;
type Spawned = EventOf<"agent_spawned">;
type Decided = EventOf<"hook_decision">["data"]["decision"];

/** subagent_stop continuations per child before its result stands (bounded like on_stop). */
const MAX_CONTINUATIONS = 3;

/** subagent_start gates agent_spawned: "allow", a deny reason, or a halt. A failure denies. */
export async function startGate(
  s: Session,
  call: Call,
): Promise<string | Halt> {
  const key = { call_id: call.data.call_id };
  const window = s.events.filter((e) => e.seq > call.seq);
  for (const ext of defining(s, "subagent_start")) {
    const prior = recorded(window, ext.name, "subagent_start", key);
    if (prior !== undefined) {
      if (prior.data.decision === "allow") continue;
      return prior.data.reason ?? prior.data.decision;
    }
    const out = await run(ext, "subagent_start", [call.data], key.call_id);
    const [decided, reason] = started(out);
    const stopped = await s.append(
      decision(ext.name, "subagent_start", decided, key, reason),
    );
    if (stopped !== undefined) return stopped;
    if (decided !== "allow") return reason ?? decided;
  }
  return "allow";
}

function started(
  out: Outcome<"subagent_start">,
): readonly [Decided, string | undefined] {
  if (out.kind === "failed") return ["failed", out.reason];
  return out.value.decision === "deny"
    ? ["deny", out.value.reason]
    : ["allow", undefined];
}

/** The subagent_stop continue reasons recorded for this child, in order. */
export function continues(s: Session, spawned: Spawned): readonly string[] {
  return s.events.flatMap((e) =>
    e.type === "hook_decision" &&
    e.data.hook === "subagent_stop" &&
    e.data.call_id === spawned.data.call_id &&
    e.data.decision === "continue"
      ? [e.data.reason ?? "Continue."]
      : [],
  );
}

/**
 * subagent_stop once a child ends: "stop" lets the result stand, "continue" sends the child
 * its reason as a new input. A failed hook stops; past the cap the result stands. A cancelled
 * child, or one whose parent is behind a cancel barrier, is final: a continue is recorded as the
 * stop it amounts to (spec/schema/README.md, "A tree cancel is final").
 */
export async function stopGate(
  s: Session,
  spawned: Spawned,
  finished: EventOf<"agent_finished">["data"],
): Promise<"stop" | "continue" | Halt> {
  const key = { call_id: spawned.data.call_id };
  const limit = final(s, finished) ? "cancelled" : undefined;
  const count = continues(s, spawned).length;
  const since = s.events.findLastIndex(
    (e) =>
      e.type === "hook_decision" &&
      e.data.hook === "subagent_stop" &&
      e.data.call_id === key.call_id &&
      e.data.decision === "continue",
  );
  const window = s.events.slice(since + 1);
  for (const ext of defining(s, "subagent_stop")) {
    if (recorded(window, ext.name, "subagent_stop", key) !== undefined)
      continue;
    const out = await run(ext, "subagent_stop", [finished], key.call_id);
    const [decided, reason] = stopped(out, count, limit);
    const halted = await s.append(
      decision(ext.name, "subagent_stop", decided, key, reason),
    );
    if (halted !== undefined) return halted;
    if (decided === "continue") return "continue";
  }
  return "stop";
}

/** Past the cap a continue is recorded as the stop it is, so a replay never re-sends it. */
function stopped(
  out: Outcome<"subagent_stop">,
  count: number,
  cancelled: "cancelled" | undefined,
): readonly [Decided, string | undefined] {
  if (out.kind === "failed") return ["failed", out.reason];
  if (out.value.decision !== "continue") return ["stop", undefined];
  if (cancelled !== undefined) return ["stop", cancelled];
  return count < MAX_CONTINUATIONS
    ? ["continue", out.value.reason]
    : ["stop", "continuation limit"];
}

/** No new work after a cancel: the child ended cancelled, or this thread is barred. */
function final(
  s: Session,
  finished: EventOf<"agent_finished">["data"],
): boolean {
  return (
    finished.status === "cancelled" ||
    (s.fold.turnOpen &&
      turnEvents(s.events, s.fold).some((e) => e.type === "cancel_requested"))
  );
}
