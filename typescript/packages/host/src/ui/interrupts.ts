import type { Json, ParkAddress } from "@threads/core/host";
import type { RunFacts } from "./facts";

// A parked run's open items as AG-UI interrupts (spec/schema/ui/README.md, "Interrupts"). Each
// answer's schema is host-api.v1.schema.json's own (a test keeps these equal to that file).

export const APPROVAL_DECISION_SCHEMA: Json = {
  type: "object",
  additionalProperties: false,
  required: ["decision"],
  properties: {
    decision: { enum: ["grant", "deny"] },
    remember_rule: {
      description:
        "grant only: appended as permission_rule_added. Must be one of the challenge's suggested_rules.",
      $ref: "urn:threads:schema:events:v1#/$defs/PermissionRule",
    },
    reason: { type: "string" },
  },
};

export const ANSWER_SCHEMA: Json = {
  description:
    "The answer to an ask_user question: text, or the chosen options.",
  type: "object",
  additionalProperties: false,
  required: ["answer"],
  properties: {
    answer: {
      oneOf: [
        { type: "string", minLength: 1 },
        { type: "array", minItems: 1, items: { type: "string" } },
      ],
    },
  },
};

/** Whether a park waits on a human's answer the UI can give (an approval or a question). */
export function answerable(address: ParkAddress): boolean {
  return address.kind === "approval" || address.kind === "input";
}

export function interrupts(
  pending: readonly ParkAddress[],
  facts: RunFacts,
): readonly Json[] {
  return pending.map((address) => interrupt(address, facts));
}

function interrupt(address: ParkAddress, facts: RunFacts): Json {
  if (address.kind === "approval") {
    const challenge = facts.challenge(address.id);
    if (challenge !== undefined)
      return {
        id: address.id,
        reason: "tool_approval",
        toolCallId: challenge.callId,
        message: `approve ${facts.call(challenge.callId)?.name ?? "the call"}`,
        responseSchema: APPROVAL_DECISION_SCHEMA,
        expiresAt: new Date(challenge.expiresAt).toISOString(),
      };
  }
  if (address.kind === "input") {
    const question = facts.call(address.id)?.input["question"];
    return {
      id: address.id,
      reason: "user_input",
      toolCallId: address.id,
      message: typeof question === "string" ? question : "",
      responseSchema: ANSWER_SCHEMA,
    };
  }
  return { id: address.id, reason: facts.parkReason(address) ?? address.kind };
}
