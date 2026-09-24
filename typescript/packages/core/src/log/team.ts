import { z } from "zod";
import { ArtifactRef, Principal } from "./common";
import { BudgetExceededData, ParkReason } from "./events/control";
import { HAS_TEXT_OR_REF } from "./events/one-of";
import {
  AskId,
  EventId,
  MailId,
  MemberName,
  MonitorId,
  RequestId,
  TeamId,
  ThreadId,
} from "./ids";
import { NonEmpty, PosInt, TimeMs } from "./primitives";
import { type Rule, type Ruled, withRule } from "./rules";
import type { Arr, EnumOf, Lit, Opt, Strict } from "./zod-types";

// Shapes shared by the team events (spec/schema/README.md, "Teams"): member references,
// provenance, results and the mail envelope. Every literal a team event records is here.

export const MemberRef: Strict<{
  tenant: typeof NonEmpty;
  team: typeof TeamId;
  name: typeof MemberName;
  generation: typeof PosInt;
}> = z
  .strictObject({
    tenant: NonEmpty,
    team: TeamId,
    name: MemberName,
    generation: PosInt.describe(
      "Changes only through a supervised restart; 1 until then.",
    ),
  })
  .meta({ id: "MemberRef", description: "One member of one team. Immutable." });
export type MemberRef = z.infer<typeof MemberRef>;

export const RequestRef: Strict<{
  thread_id: typeof ThreadId;
  event_id: typeof EventId;
}> = z.strictObject({ thread_id: ThreadId, event_id: EventId }).meta({
  id: "RequestRef",
  description: "An event in another thread's log, by thread and event id.",
});

export const Provenance: Strict<{
  principal: typeof Principal;
  root_request: typeof RequestRef;
  via: Arr<typeof MemberRef>;
}> = z
  .strictObject({
    principal: Principal.describe(
      "The verified originating principal: the root run's input principal, or the operator's.",
    ),
    root_request: RequestRef.describe(
      "The lead run's user_input, or the team log's operator_request. Budgets roll up to its run budget.",
    ),
    via: z
      .array(MemberRef)
      .describe(
        "The member hops. Provenance only: authority, credentials and grants never transfer.",
      ),
  })
  .meta({
    id: "Provenance",
    description:
      "Causal, not structural: who originated the work and which request it belongs to. One turn's opening batch shares one (principal, root_request).",
  });
export type Provenance = z.infer<typeof Provenance>;

export const TextBody: Ruled<
  Strict<{ text: Opt<z.ZodString>; ref: Opt<typeof ArtifactRef> }>,
  typeof HAS_TEXT_OR_REF
> = withRule(
  z.strictObject({ text: z.string().optional(), ref: ArtifactRef.optional() }),
  HAS_TEXT_OR_REF,
  {
    id: "TextBody",
    description:
      "Inline text up to the inline cap, else a content-addressed artifact written before the append.",
  },
);

// The host API RunErrorCode, then the two rebind codes.
const MEMBER_ERROR_CODES = [
  "model_unavailable",
  "context_exhausted",
  "max_output",
  "max_turns",
  "output_invalid",
  "input_denied",
  "stop_hook_limit",
  "model_error",
  "content_unsupported",
  "continuation_unsupported",
  "transport_fence_unsupported",
  "secret_in_provider_output",
  "secret_in_stored_bytes",
  "artifact_missing",
  "artifact_corrupt",
  "unmatched_external_op",
  "branch_busy",
  "branch_not_runnable",
  "pin_unavailable",
  "pin_mismatch",
] as const;
export const MemberErrorCode: EnumOf<typeof MEMBER_ERROR_CODES> = z
  .enum(MEMBER_ERROR_CODES)
  .meta({
    id: "MemberErrorCode",
    description:
      "A failed member's code: the host API RunErrorCode, plus pin_unavailable and pin_mismatch for a failed rebind.",
  });

type Result<S extends string, F extends z.core.$ZodLooseShape> = Strict<
  { member: typeof MemberRef; status: Lit<S> } & F
>;

export const CompletedResult: Result<"completed", { output: typeof TextBody }> =
  z
    .strictObject({
      member: MemberRef,
      status: z.literal("completed"),
      output: TextBody.describe(
        "The latest task's answer; structured output is its accepted value as RFC 8785 JSON text.",
      ),
    })
    .meta({ id: "CompletedResult" });
export const FailedResult: Result<
  "failed",
  { error: Strict<{ code: typeof MemberErrorCode; message: z.ZodString }> }
> = z
  .strictObject({
    member: MemberRef,
    status: z.literal("failed"),
    error: z.strictObject({ code: MemberErrorCode, message: z.string() }),
  })
  .meta({ id: "FailedResult" });
export const CancelledResult: Result<"cancelled", Record<never, never>> = z
  .strictObject({ member: MemberRef, status: z.literal("cancelled") })
  .meta({ id: "CancelledResult" });
export const ExhaustedResult: Result<
  "budget_exhausted",
  { budget: typeof BudgetExceededData }
> = z
  .strictObject({
    member: MemberRef,
    status: z.literal("budget_exhausted"),
    budget: BudgetExceededData,
  })
  .meta({ id: "ExhaustedResult" });
export const HandedOffResult: Result<
  "handed_off",
  { to_thread: typeof ThreadId }
> = z
  .strictObject({
    member: MemberRef,
    status: z.literal("handed_off"),
    to_thread: ThreadId.describe("The lead only: members can't hand off."),
  })
  .meta({ id: "HandedOffResult" });

/** An ended member's result: everything but completed, which leaves a member idle. */
export const EndedResult: z.ZodDiscriminatedUnion<
  [
    typeof FailedResult,
    typeof CancelledResult,
    typeof ExhaustedResult,
    typeof HandedOffResult,
  ],
  "status"
> = z
  .discriminatedUnion("status", [
    FailedResult,
    CancelledResult,
    ExhaustedResult,
    HandedOffResult,
  ])
  .meta({ id: "EndedResult" });

/** A settled member's result as logged: completed output is inline text or an artifact ref. */
export const StoredMemberResult: z.ZodDiscriminatedUnion<
  [
    typeof CompletedResult,
    typeof FailedResult,
    typeof CancelledResult,
    typeof ExhaustedResult,
    typeof HandedOffResult,
  ],
  "status"
> = z
  .discriminatedUnion("status", [
    CompletedResult,
    FailedResult,
    CancelledResult,
    ExhaustedResult,
    HandedOffResult,
  ])
  .meta({
    id: "StoredMemberResult",
    description:
      "What a settled member returned, as member_idle, member_ended, notifications and member_observed record it. Public result APIs hydrate a ref'd output (sha256 and length verified); the audit feed returns it as stored.",
  });
export type StoredMemberResult = z.infer<typeof StoredMemberResult>;

// Refusals decided under a writer are logged; busy never is (no writer was acquired).
const TEAM_REFUSALS = [
  "forbidden",
  "unknown_agent",
  "concurrency_cap",
  "budget_exceeded",
  "team_closed",
  "invalid_definition",
  "unknown_member",
  "stale_member",
  "member_ended",
  "self",
  "mailbox_full",
  "idempotency_key_reused",
  "idempotency_key_principal_mismatch",
] as const;
export const TeamRefusal: EnumOf<typeof TEAM_REFUSALS> = z
  .enum(TEAM_REFUSALS)
  .meta({
    id: "TeamRefusal",
    description:
      "A refusal an operator request records as operator_refused. A model call records its refusal in its own tool_result.",
  });
export type TeamRefusal = z.infer<typeof TeamRefusal>;

const BOUNCE_CODES = ["stale_member", "member_ended"] as const;
export const BounceCode: EnumOf<typeof BOUNCE_CODES> = z
  .enum(BOUNCE_CODES)
  .meta({
    id: "BounceCode",
    description:
      "Why a recipient refused pending mail: it was for an older generation, or the member ended.",
  });

export const MAIL_KINDS = [
  "message",
  "ask",
  "reply",
  "task",
  "cancel",
  "member_settled",
  "member_parked",
  "member_ended",
  "bounce",
] as const;

export const MailAddress: z.ZodXor<
  readonly [
    Strict<{ name: typeof MemberName; generation: typeof PosInt }>,
    Lit<"team_log">,
  ]
> = z
  .xor([
    z.strictObject({ name: MemberName, generation: PosInt }),
    z.literal("team_log"),
  ])
  .meta({
    id: "MailAddress",
    description:
      "A member at one generation, or the team log (the operator's replies and notifications).",
  });

export const MailSender: z.ZodXor<
  readonly [typeof MemberRef, Strict<{ operator: typeof RequestId }>]
> = z
  .xor([
    MemberRef,
    z.strictObject({ operator: RequestId }).meta({ id: "OperatorSender" }),
  ])
  .meta({
    id: "MailSender",
    description:
      "The sending member, or the operator request. Written by the fenced writer; never an actor.",
  });

// A field appears only on the kinds that use it, and each kind carries what it needs. Typed as
// a plain Rule: narrowing the TS type by fifteen if/then branches would be exponential.
const only = (field: string, kinds: readonly string[]): Rule => ({
  if: { required: [field] },
  then: { properties: { kind: { enum: kinds } } },
});
const needs = (kinds: readonly string[], fields: readonly string[]): Rule => ({
  if: { properties: { kind: { enum: kinds } } },
  then: { required: fields },
});
const MEMBER_SENT = [
  "reply",
  "member_settled",
  "member_parked",
  "member_ended",
  "bounce",
] as const;
const ENVELOPE_RULE: Rule = {
  allOf: [
    only("body", ["message", "ask", "reply", "task"]),
    only("ask_id", ["ask", "reply", "bounce"]),
    only("deadline", ["ask"]),
    only("monitor_id", ["member_settled", "member_parked", "member_ended"]),
    only("reason", ["member_parked"]),
    only("result", ["member_settled", "member_ended", "bounce"]),
    only("code", ["bounce"]),
    needs(["message", "task"], ["body"]),
    needs(["ask"], ["body", "ask_id", "deadline"]),
    needs(["reply"], ["body", "ask_id"]),
    needs(["member_settled", "member_ended"], ["monitor_id", "result"]),
    needs(["member_parked"], ["monitor_id", "reason"]),
    needs(["bounce"], ["code"]),
    // An open ask's bounce closes it member_ended, so it carries the ended member's result.
    {
      if: { properties: { kind: { const: "bounce" } }, required: ["ask_id"] },
      then: { required: ["result"] },
    },
    // Only a member replies, notifies or bounces; the team log receives only those.
    {
      if: { properties: { kind: { enum: MEMBER_SENT } } },
      then: { properties: { from: { required: ["name"] } } },
    },
    {
      if: { properties: { to: { const: "team_log" } } },
      then: { properties: { kind: { enum: MEMBER_SENT } } },
    },
    {
      if: { properties: { kind: { const: "member_settled" } } },
      then: {
        properties: {
          result: { properties: { status: { const: "completed" } } },
        },
      },
    },
    {
      if: { properties: { kind: { const: "member_ended" } } },
      then: {
        properties: {
          result: { not: { properties: { status: { const: "completed" } } } },
        },
      },
    },
  ],
};

export const MailEnvelope: Ruled<
  Strict<{
    mail_id: typeof MailId;
    kind: EnumOf<typeof MAIL_KINDS>;
    team: typeof TeamId;
    from: typeof MailSender;
    to: typeof MailAddress;
    provenance: typeof Provenance;
    causal: typeof RequestRef;
    ask_id: Opt<typeof AskId>;
    monitor_id: Opt<typeof MonitorId>;
    deadline: Opt<typeof TimeMs>;
    reason: Opt<typeof ParkReason>;
    code: Opt<typeof BounceCode>;
    result: Opt<typeof StoredMemberResult>;
    body: Opt<typeof TextBody>;
  }>,
  Rule
> = withRule(
  z.strictObject({
    mail_id: MailId,
    kind: z.enum(MAIL_KINDS),
    team: TeamId,
    from: MailSender,
    to: MailAddress,
    provenance: Provenance,
    causal: RequestRef.describe(
      "The request or event that caused this mail: the call's tool_call, the operator_request, the member's idle or end event, or the refused mail's mail_refused.",
    ),
    ask_id: AskId.describe(
      "ask: its own mail_id. reply: the ask it answers. bounce: the open ask it closes as member_ended.",
    ).optional(),
    monitor_id: MonitorId.describe(
      "The monitor this notification fires; its row is deleted in the same append.",
    ).optional(),
    deadline: TimeMs.describe("ask: now + timeoutMs.").optional(),
    reason: ParkReason.describe(
      "member_parked: the member's park reason.",
    ).optional(),
    code: BounceCode.optional(),
    result: StoredMemberResult.describe(
      "member_settled (completed), member_ended (terminal), or an ask's bounce (the ended member's result).",
    ).optional(),
    body: TextBody.optional(),
  }),
  ENVELOPE_RULE,
  {
    id: "MailEnvelope",
    description:
      "One mail, as the sender's message_sent records it and every receipt copies it byte for byte. member_parked is a one-time, non-consuming park notice.",
  },
);
export type MailEnvelope = z.infer<typeof MailEnvelope>;
