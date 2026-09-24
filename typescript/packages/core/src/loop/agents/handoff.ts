import { type EventOf, loopPending } from "../../fold/state";
import { ThreadId } from "../../log";
import { uuidv7 } from "../../store/encode";
import { HandoffInput } from "../../tools/agent-inputs";
import { draft, TOOL } from "../drafts";
import { contextPolicy } from "../policy";
import type { Session } from "../session";
import { BARRED, type Halt } from "../types";
import { handoffTranscript } from "./transcript";

// handoff: the conversation moves to a new thread of a listed agent. This
// thread records handoff, the call's result and turn_completed{handoff}, and takes no input
// after it; the host starts the target from the recorded handoff, so a restart finds it.

type Call = EventOf<"tool_call">;

export function handOff(s: Session, call: Call): Halt | undefined {
  const { call_id } = call.data;
  const { agent } = HandoffInput.parse(call.data.input);
  // A target the source didn't list fails before anything happens.
  if (!(s.fold.policy?.handoffs ?? []).includes(agent))
    return s.append(
      draft.toolResult(
        {
          call_id,
          is_error: true,
          origin: "not_executed",
          preview: `not a handoff target: ${agent}`,
        },
        TOOL,
      ),
    );
  // The turn ends here: calls after the handoff in the same response never run.
  const others = loopPending(s.fold).filter((id) => id !== call_id);
  const done = s.appendWork(
    {
      type: "handoff",
      type_version: 1,
      critical: true,
      actor: { kind: "host" },
      data: {
        call_id,
        to_agent: agent,
        to_thread_id: ThreadId.parse(uuidv7(s.now())),
        forwarded: "transcript",
        forwarded_ref: s.store(
          handoffTranscript(
            s.events,
            contextPolicy(s.fold.policy).spill.threshold_bytes,
          ),
          "text/plain",
        ),
      },
    },
    draft.toolResult(
      {
        call_id,
        is_error: false,
        origin: "executed",
        preview: `Handed off to ${agent}.`,
      },
      TOOL,
    ),
    ...others.map((id) =>
      draft.toolResult(
        {
          call_id: id,
          is_error: true,
          origin: "not_executed",
          preview: "not executed: handed off",
        },
        { kind: "host" },
      ),
    ),
    draft.turnCompleted("handoff"),
  );
  // A cancel landed first: no target starts; the cancellation step closes the call.
  return done === BARRED ? undefined : done;
}
