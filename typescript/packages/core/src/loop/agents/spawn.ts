import type { EventOf } from "../../fold/state";
import { type Principal, ThreadId } from "../../log";
import { uuidv7 } from "../../store/encode";
import { SpawnAgentInput } from "../../tools/agent-inputs";
import { draft, TOOL } from "../drafts";
import { inheritedBy } from "../ledger";
import type { Session } from "../session";
import { recordOutput } from "../spill";
import type { ChildDone, ChildEnd, Halt } from "../types";
import { continues, startGate, stopGate } from "./gates";
import { parkOn } from "./park";
import { teamOf } from "./team";

// spawn_agent. agent_spawned is durable before the child's thread_started, so
// a restarted parent finds the child by its id and resumes it instead of starting another: the
// call is re-dispatched, sees agent_spawned, and runs the same child thread to its end. The
// child's own effects are settled by the child's recovery, never repeated by the parent.

type Call = EventOf<"tool_call">;
export type Spawned = EventOf<"agent_spawned">;

export async function spawnAgent(
  s: Session,
  call: Call,
): Promise<Halt | undefined> {
  const spawned = spawnedFor(s, call.data.call_id);
  if (spawned === undefined) return start(s, call);
  if (spawned.data.mode === "background") return background(s, spawned);
  const end = await rounds(s, spawned);
  if ("code" in end) return end;
  return end.status === "parked"
    ? parkOn(s, spawned, end.reason)
    : finish(s, spawned, end, false);
}

export function spawnedFor(s: Session, callId: string): Spawned | undefined {
  return s.events.find(
    (e): e is Spawned =>
      e.type === "agent_spawned" && e.data.call_id === callId,
  );
}

/** Checks, the subagent_start gate, then agent_spawned; the next dispatch runs the child. */
async function start(s: Session, call: Call): Promise<Halt | undefined> {
  const { call_id } = call.data;
  const input = SpawnAgentInput.parse(call.data.input);
  const sub = s.config.agents?.subagent(input.agent);
  const isolation = input.isolation ?? "none";
  const refused =
    sub === undefined
      ? `unknown agent: ${input.agent}`
      : isolation !== "none"
        ? `isolation ${isolation} is not supported yet; use none`
        : undefined;
  if (refused !== undefined) return closed(s, call_id, "not_executed", refused);
  const denied = await startGate(s, call);
  if (typeof denied !== "string") return denied;
  if (denied !== "allow") return closed(s, call_id, "denied", denied);
  const stopped = s.append(
    draft.agentSpawned({
      call_id,
      child_thread_id: ThreadId.parse(uuidv7(s.now())),
      agent_name: input.agent,
      mode: input.background === true ? "background" : "foreground",
      isolation,
      ...(sub?.budget === undefined ? {} : { budget: sub.budget }),
    }),
  );
  return stopped ?? spawnAgent(s, call);
}

/** A background child answers its call at once; its result arrives as tool_result_late. */
function background(s: Session, spawned: Spawned): Halt | undefined {
  const { call_id } = spawned.data;
  const stopped = s.fold.pending.has(call_id)
    ? s.append(
        draft.toolResult(
          {
            call_id,
            is_error: false,
            origin: "deferred",
            preview: `${spawned.data.agent_name} started in the background`,
          },
          TOOL,
        ),
      )
    : undefined;
  if (stopped === undefined) launch(s, spawned);
  return stopped;
}

/** Runs a background child in this process; the loop records its end at a step boundary. */
export function launch(s: Session, spawned: Spawned): void {
  const { call_id } = spawned.data;
  if (s.background.has(call_id)) return;
  const running = (async () => {
    s.finished.set(call_id, await runChild(s, spawned));
  })();
  s.background.set(call_id, running);
}

/** Foreground: run the child, then subagent_stop, until the hooks let its result stand. */
async function rounds(s: Session, spawned: Spawned): Promise<ChildEnd | Halt> {
  for (;;) {
    const end = await runChild(s, spawned);
    if ("code" in end || end.status === "parked") return end;
    const next = await stopGate(s, spawned, finished(s, spawned, end));
    if (next !== "continue") return next === "stop" ? end : next;
  }
}

/** The prompt, then every recorded subagent_stop continue reason for this child. */
function inputsOf(s: Session, spawned: Spawned): readonly string[] {
  const tool = s.events.find(
    (e): e is Call =>
      e.type === "tool_call" && e.data.call_id === spawned.data.call_id,
  );
  if (tool === undefined) throw new Error("agent_spawned follows its call");
  return [
    SpawnAgentInput.parse(tool.data.input).prompt,
    ...continues(s, spawned),
  ];
}

export async function runChild(
  s: Session,
  spawned: Spawned,
  cancel?: Principal,
): Promise<ChildEnd | Halt> {
  const sub = s.config.agents?.subagent(spawned.data.agent_name);
  if (sub === undefined)
    return {
      status: "failed",
      output: `agent ${spawned.data.agent_name} is not configured`,
      usage: { input_tokens: null, output_tokens: null },
    };
  return sub.run({
    threadId: spawned.data.child_thread_id,
    parent: {
      thread_id: s.threadId,
      branch_id: s.branchId,
      event_id: spawned.event_id,
    },
    inputs: inputsOf(s, spawned),
    ceiling: (call) => s.config.authorize(call, s.fold),
    tools: new Set(s.fold.tools.map((t) => t.name)),
    team: teamOf(s),
    covering: inheritedBy(s),
    ...(cancel === undefined ? {} : { cancel }),
  });
}

export function finished(
  s: Session,
  spawned: Spawned,
  end: ChildDone,
): EventOf<"agent_finished">["data"] {
  return {
    child_thread_id: spawned.data.child_thread_id,
    status: end.status,
    output_ref: s.store(end.output, "text/plain"),
    usage: end.usage,
  };
}

/** The child's one agent_finished and the call's result, in one append (F7.5). */
export function finish(
  s: Session,
  spawned: Spawned,
  end: ChildDone,
  late: boolean,
): Halt | undefined {
  const { call_id } = spawned.data;
  const text =
    end.status === "completed" ? end.output : `${end.status}: ${end.output}`;
  const shown = recordOutput(s, call_id, text);
  const result = {
    call_id,
    is_error: end.status !== "completed",
    preview: shown.preview,
    ...(shown.ref === undefined ? {} : { ref: shown.ref }),
  };
  return s.append(
    draft.agentFinished(finished(s, spawned, end)),
    late
      ? draft.toolResultLate(result)
      : draft.toolResult({ ...result, origin: "executed" }, TOOL),
  );
}

function closed(
  s: Session,
  call_id: Call["data"]["call_id"],
  origin: "not_executed" | "denied",
  preview: string,
): Halt | undefined {
  return s.append(
    draft.toolResult({ call_id, is_error: true, origin, preview }, TOOL),
  );
}
