import { z } from "zod";
import { ArtifactRef } from "../common";
import { type EventDef, event } from "../envelope";
import { CallId } from "../ids";
import { Name, NonEmpty } from "../primitives";
import type { EnumOf, Opt, Strict } from "../zod-types";

// A2A: the partner card a thread pinned, the calls it makes to that partner, and the task states
// it observes back (spec/schema/README.md, semantic rules 56-58).

const BINDINGS = ["JSONRPC", "HTTP+JSON"] as const;
const OPERATIONS = ["send_message"] as const;
const TASK_STATES = [
  "TASK_STATE_SUBMITTED",
  "TASK_STATE_WORKING",
  "TASK_STATE_COMPLETED",
  "TASK_STATE_FAILED",
  "TASK_STATE_CANCELED",
  "TASK_STATE_INPUT_REQUIRED",
  "TASK_STATE_REJECTED",
  "TASK_STATE_AUTH_REQUIRED",
] as const;

export const RemoteCardData: Strict<{
  remote: typeof Name;
  card_ref: typeof ArtifactRef;
  interface_url: typeof NonEmpty;
  binding: EnumOf<typeof BINDINGS>;
}> = z.strictObject({
  remote: Name.describe("The partner, as this thread's config names it."),
  card_ref: ArtifactRef.describe("The agent card's exact fetched bytes."),
  interface_url: NonEmpty.describe(
    "The supportedInterfaces entry this thread pinned: the only endpoint it calls.",
  ),
  binding: z.enum(BINDINGS).describe("The transport that entry declares."),
});
export const RemoteCard: EventDef<"remote_card", typeof RemoteCardData, true> =
  event({
    type: "remote_card",
    critical: true,
    description:
      "Pins a partner's card by hash for one thread. A card that changes later never moves a conversation already under way: the thread keeps calling the pinned interface_url with the pinned binding, and a new card is a new thread's pin.",
    data: RemoteCardData,
  });

export const RemoteCallData: Strict<{
  call_id: typeof CallId;
  remote: typeof Name;
  operation: EnumOf<typeof OPERATIONS>;
  message_id: typeof NonEmpty;
  context_id: typeof NonEmpty;
  task_id: Opt<typeof NonEmpty>;
  request_ref: typeof ArtifactRef;
}> = z.strictObject({
  call_id: CallId,
  remote: Name.describe("The partner whose card this thread pinned."),
  operation: z.enum(OPERATIONS),
  message_id: NonEmpty.describe(
    "Derived from (branch_id, call_id) alone, never from the attempt.",
  ),
  context_id: NonEmpty.describe("The A2A conversation this call continues."),
  task_id: NonEmpty.describe(
    "The partner's task this call adds to; absent when the call opens one.",
  ).optional(),
  request_ref: ArtifactRef.describe(
    "The exact request body, stored after redaction.",
  ),
});
export const RemoteCall: EventDef<"remote_call", typeof RemoteCallData, true> =
  event({
    type: "remote_call",
    critical: true,
    description:
      "Appended in the same append as its effect_begin (rule 56), before any byte leaves. request_ref holds the exact bytes, so a resend after safe_to_retry is byte-identical and carries the same messageId. message_id is derived from (branch_id, call_id) alone and never includes the attempt, so every attempt of one call is one message to any peer that deduplicates. One remote_call per call_id (rule 58): every effect_begin for it names those same stored bytes.",
    data: RemoteCallData,
  });

export const RemoteTaskStateData: Strict<{
  call_id: typeof CallId;
  task_id: typeof NonEmpty;
  state: EnumOf<typeof TASK_STATES>;
  status_ref: Opt<typeof ArtifactRef>;
  artifacts_ref: Opt<typeof ArtifactRef>;
}> = z.strictObject({
  call_id: CallId.describe("The remote_call whose effect committed (rule 57)."),
  task_id: NonEmpty,
  state: z.enum(TASK_STATES).describe("The partner's state name, verbatim."),
  status_ref: ArtifactRef.describe(
    "The status update's exact bytes, when the partner sent one.",
  ).optional(),
  artifacts_ref: ArtifactRef.describe(
    "The artifacts of this observation, as one canonical JSON body.",
  ).optional(),
});
export const RemoteTaskState: EventDef<
  "remote_task_state",
  typeof RemoteTaskStateData,
  false
> = event({
  type: "remote_task_state",
  critical: false,
  description:
    "An observation of a partner's task, deduplicated by state and content hash: a repeat of the same state with the same bytes is not appended twice. Not critical, because it records what we saw and never what we decided: a reader that does not know it can skip it and still reduce the log.",
  data: RemoteTaskStateData,
});
