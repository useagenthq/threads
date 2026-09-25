import { z } from "zod";
import { ParkReason } from "./events/control";
import {
  AskId,
  BranchId,
  MailId,
  MemberName,
  MonitorId,
  RequestId,
  TeamId,
  ThreadId,
} from "./ids";
import { Name, PosInt, TimeMs } from "./primitives";
import { type Rule, type Ruled, withRule } from "./rules";
import {
  MemberRef,
  Provenance,
  RequestRef,
  StoredMemberResult,
  TextBody,
  TurnFailure,
} from "./team";
import type { EnumOf, Lit, Opt, Strict } from "./zod-types";

// Team mail (spec/schema/README.md, "Teams"): the bounce codes, the addresses (members, the team
// log and, in Teams Phase 2, callers) and the envelope every message_sent records and every
// receipt copies.

const BOUNCE_CODES = ["stale_member", "member_ended"] as const;
const MAIL_BOUNCE_CODES = [
  "stale_member",
  "member_ended",
  "turn_failed",
] as const;
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

/** A thread outside the host team that sends and asks host members (Phase 2). */
export const CallerAddress: Strict<{
  caller: Strict<{
    thread_id: typeof ThreadId;
    branch_id: typeof BranchId;
    agent: typeof Name;
  }>;
}> = z
  .strictObject({
    caller: z.strictObject({
      thread_id: ThreadId,
      branch_id: BranchId,
      agent: Name.describe(
        "The caller thread's agent: a host rule's from, and the deciding rule's key.",
      ),
    }),
  })
  .meta({
    id: "CallerAddress",
    description:
      "A thread outside the host team (Phase 2). It may only send and ask a host member, and a host member answers it only by reply or bounce.",
  });

export const MailAddress: z.ZodXor<
  readonly [
    Strict<{ name: typeof MemberName; generation: typeof PosInt }>,
    Lit<"team_log">,
    typeof CallerAddress,
  ]
> = z
  .xor([
    z.strictObject({ name: MemberName, generation: PosInt }),
    z.literal("team_log"),
    CallerAddress,
  ])
  .meta({
    id: "MailAddress",
    description:
      "A member at one generation, the team log (the operator's replies and notifications), or a caller thread outside the host team.",
  });

export const MailSender: z.ZodXor<
  readonly [
    typeof MemberRef,
    Strict<{ operator: typeof RequestId }>,
    typeof CallerAddress,
  ]
> = z
  .xor([
    MemberRef,
    z.strictObject({ operator: RequestId }).meta({ id: "OperatorSender" }),
    CallerAddress,
  ])
  .meta({
    id: "MailSender",
    description:
      "The sending member, the operator request, or a caller thread. Written by the fenced writer; never an actor.",
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
    only("error", ["bounce"]),
    needs(["message", "task"], ["body"]),
    needs(["ask"], ["body", "ask_id", "deadline"]),
    needs(["reply"], ["body", "ask_id"]),
    needs(["member_settled", "member_ended"], ["monitor_id", "result"]),
    needs(["member_parked"], ["monitor_id", "reason"]),
    needs(["bounce"], ["code"]),
    // An open ask's bounce closes it member_ended, so it carries the ended member's result.
    {
      if: {
        properties: {
          kind: { const: "bounce" },
          code: { enum: BOUNCE_CODES },
        },
        required: ["ask_id"],
      },
      then: { required: ["result"] },
    },
    // A host member's failed turn bounces each ask it took: the ask closes failed with error.
    {
      if: {
        properties: { code: { const: "turn_failed" } },
        required: ["code"],
      },
      then: { required: ["ask_id", "error"], not: { required: ["result"] } },
    },
    {
      if: { required: ["error"] },
      then: { properties: { code: { const: "turn_failed" } } },
    },
    // A caller only sends and asks; a host member answers a caller only by reply or bounce.
    {
      if: { properties: { from: { required: ["caller"] } } },
      then: { properties: { kind: { enum: ["message", "ask"] } } },
    },
    {
      if: {
        properties: {
          to: { not: { const: "team_log" }, required: ["caller"] },
        },
      },
      then: { properties: { kind: { enum: ["reply", "bounce"] } } },
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
    code: Opt<EnumOf<typeof MAIL_BOUNCE_CODES>>;
    error: Opt<typeof TurnFailure>;
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
      "ask: its own mail_id. reply: the ask it answers. bounce: the open ask it closes as member_ended, or as failed for turn_failed.",
    ).optional(),
    monitor_id: MonitorId.describe(
      "The monitor this notification fires; its row is deleted in the same append.",
    ).optional(),
    deadline: TimeMs.describe("ask: now + timeoutMs.").optional(),
    reason: ParkReason.describe(
      "member_parked: the member's park reason.",
    ).optional(),
    code: z
      .enum(MAIL_BOUNCE_CODES)
      .describe(
        "bounce: why. stale_member and member_ended answer a mail_refused; turn_failed (Phase 2) answers an ask a host member's failed turn took.",
      )
      .optional(),
    error: TurnFailure.describe(
      "turn_failed only: why the host member's turn failed; the ask closes failed with it.",
    ).optional(),
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
