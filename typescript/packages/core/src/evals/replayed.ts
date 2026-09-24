import type { z } from "zod";
import type { EventOf } from "../fold/state";
import type { KnownEvent, ModelRef } from "../log";
import type { Authorization } from "../loop";
import { type Model, type ScriptedModel, scriptedModel } from "../model";
import { markTestKit } from "../model/guard";

// What a rerun plays back instead of the user's world: the recorded model replies, under the
// settings each epoch pinned, and the recorded permission decisions. Nothing here reaches a
// provider: every model it makes is test kit.

const INPUTS = ["text", "image_ref", "document_ref", "audio_ref"] as const;

export type Replayed = {
  readonly models: (ref: z.infer<typeof ModelRef>) => Model;
  readonly script: ScriptedModel;
};

/**
 * One script played by every settings epoch. For a recorded turn each epoch is seen as the
 * model the log pinned for it, accepting every input the recording sent, so the loop's
 * capability and limit checks decide as they did then; the corpus plays the scripted model.
 */
export function replayedModels(
  script: unknown,
  events: () => readonly KnownEvent[],
  asRecorded: boolean,
): Replayed {
  const played = scriptedModel(script);
  const made = new Map<string, Model>();
  const models = (ref: z.infer<typeof ModelRef>): Model => {
    if (!asRecorded) return played;
    const key = `${ref.provider}/${ref.name}`;
    const known = made.get(key);
    if (known !== undefined) return known;
    const policy = events().findLast((e) => e.type === "thread_started");
    const limits = (
      policy?.type === "thread_started"
        ? (policy.data.policy?.models ?? [])
        : []
    ).find((m) => m.provider === ref.provider && m.name === ref.name);
    const model: Model = {
      info: {
        ...played.info,
        model: ref,
        accepts: INPUTS,
        ...(limits === undefined ? {} : { limits }),
      },
      send: played.send,
    };
    markTestKit(model);
    made.set(key, model);
    return model;
  };
  return { models, script: played };
}

type Call = EventOf<"tool_call">;

/**
 * The recorded permission decisions as the policy's answers. A decision a hook made records
 * no policy answer: the policy then allowed, or asked when a permission_request hook answered.
 */
export function recordedAuthorizer(
  recorded: readonly KnownEvent[] | undefined,
): (call: Call) => Authorization {
  return (call) => {
    const callId = call.data.call_id;
    const decided = recorded?.find(
      (e): e is EventOf<"permission_decision"> =>
        e.type === "permission_decision" && e.data.call_id === callId,
    );
    if (decided === undefined)
      return {
        decision: "allow",
        source: "policy",
        rule_id: "conformance_allow",
      };
    const { decision, source, rule_id } = decided.data;
    if (source !== "hook")
      return {
        decision,
        source,
        ...(rule_id === undefined ? {} : { rule_id }),
      };
    const asked = recorded?.some(
      (e) =>
        e.type === "hook_decision" &&
        e.data.hook === "permission_request" &&
        e.data.call_id === callId,
    );
    return { decision: asked === true ? "ask" : "allow", source: "policy" };
  };
}
