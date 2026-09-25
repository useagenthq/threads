import { z } from "zod";
import { ActorWithPrincipal, Principal } from "../common";
import { type EventDef, event, eventWithActor } from "../envelope";
import { AskId, CallId, MailId, RequestId } from "../ids";
import { BounceCode, MailEnvelope } from "../mail";
import { Name, NonEmpty, Sha256 } from "../primitives";
import { type Ruled, withRule } from "../rules";
import { EndedResult, Provenance, TeamRefusal, TurnFailure } from "../team";
import type { EnumOf, Lit, Opt, Strict } from "../zod-types";
import { InvalidDefinition } from "./team";

// Mail between team members and the operator, asks, and the decisions around them
// (spec/schema/README.md, "Teams"). The sender's writer inserts a mail row with its
// message_sent; only the recipient's writer changes it.

export const MessageSentData: Strict<{ envelope: typeof MailEnvelope }> =
  z.strictObject({ envelope: MailEnvelope });
export const MessageSent: EventDef<
  "message_sent",
  typeof MessageSentData,
  true
> = event({
  type: "message_sent",
  critical: true,
  description:
    "Mail, in the sender's log: inserts its pending mail row (and for an ask, its open asks row).",
  data: MessageSentData,
});

export const MessageReceivedData: Strict<{
  mail_id: typeof MailId;
  envelope: typeof MailEnvelope;
}> = z.strictObject({
  mail_id: MailId,
  envelope: MailEnvelope.describe(
    "Byte for byte the sender's message_sent envelope, provenance included, so every rule about received mail reads only this log.",
  ),
});
export const MessageReceived: EventDef<
  "message_received",
  typeof MessageReceivedData,
  true,
  typeof ActorWithPrincipal
> = eventWithActor({
  type: "message_received",
  critical: true,
  description:
    "The recipient consumed a mail (its row becomes consumed). Actor {host, provenance.principal}. It opens a turn only as the Teams rules say, and renders as an untrusted <message> user line.",
  data: MessageReceivedData,
  actor: ActorWithPrincipal,
});

export const MailRefusedData: Strict<{
  mail_id: typeof MailId;
  code: typeof BounceCode;
}> = z.strictObject({ mail_id: MailId, code: BounceCode });
export const MailRefused: EventDef<
  "mail_refused",
  typeof MailRefusedData,
  true
> = event({
  type: "mail_refused",
  critical: true,
  description:
    "The recipient refused a pending mail (its row becomes stale for stale_member, returned for member_ended); a bounce to the sender goes in the same append.",
  data: MailRefusedData,
});

const answered = z.strictObject({
  status: z.literal("answered"),
  reply: MailId.describe(
    "The reply mail, received in this same append; its text is the answer.",
  ),
});
const timedOut = z.strictObject({ status: z.literal("timed_out") });
const memberEnded = z.strictObject({
  status: z.literal("member_ended"),
  result: EndedResult,
});
const askCancelled = z.strictObject({ status: z.literal("cancelled") });
const askFailed = z.strictObject({
  status: z.literal("failed"),
  error: TurnFailure.describe(
    "Byte for byte the turn_failed bounce's error, received in this same append (Phase 2: a host member's turn failed).",
  ),
});
export const AskClosedData: Strict<{
  ask_id: typeof AskId;
  outcome: z.ZodDiscriminatedUnion<
    [
      Strict<{ status: Lit<"answered">; reply: typeof MailId }>,
      Strict<{ status: Lit<"timed_out"> }>,
      Strict<{
        status: Lit<"member_ended">;
        result: typeof EndedResult;
      }>,
      Strict<{ status: Lit<"cancelled"> }>,
      Strict<{ status: Lit<"failed">; error: typeof TurnFailure }>,
    ],
    "status"
  >;
}> = z.strictObject({
  ask_id: AskId,
  outcome: z
    .discriminatedUnion("status", [
      answered,
      timedOut,
      memberEnded,
      askCancelled,
      askFailed,
    ])
    .describe(
      "Decided in order: a reply, then a bounce naming the ask (member_ended, or failed for a host member's turn_failed), then a cancel or team close, else the deadline.",
    ),
});
export const AskClosed: EventDef<"ask_closed", typeof AskClosedData, true> =
  event({
    type: "ask_closed",
    critical: true,
    description:
      "In the asker's log (a member, or the team log): closes the open ask. A member asker also appends resumed and the ask call's one tool_result.",
    data: AskClosedData,
  });

const OPERATOR_OPS = ["start", "send", "ask", "wait", "cancel"] as const;
export const OperatorRequestData: Strict<{
  request_id: typeof RequestId;
  op: EnumOf<typeof OPERATOR_OPS>;
  principal: typeof Principal;
  idempotency_key: Opt<typeof NonEmpty>;
  body_hash: typeof Sha256;
  provenance: typeof Provenance;
}> = z.strictObject({
  request_id: RequestId,
  op: z.enum(OPERATOR_OPS),
  principal: Principal,
  idempotency_key: NonEmpty.describe(
    "Scoped to this team and op; inserts the operator_receipts row.",
  ).optional(),
  body_hash: Sha256.describe("sha256 of the request's canonical JSON body."),
  provenance: Provenance.describe(
    "root_request is this event: it is the root request for budgets.",
  ),
});
export const OperatorRequest: EventDef<
  "operator_request",
  typeof OperatorRequestData,
  true,
  typeof ActorWithPrincipal
> = eventWithActor({
  type: "operator_request",
  critical: true,
  description:
    "One team.start/send/ask/wait/cancel call, in the team log, with the op's own events or its refusal in the same append.",
  data: OperatorRequestData,
  actor: ActorWithPrincipal,
});

const OPERATOR_REFUSED_RULE = {
  if: { properties: { code: { const: "invalid_definition" } } },
  then: { required: ["detail"] },
  else: { not: { required: ["detail"] } },
} as const;
export const OperatorRefusedData: Ruled<
  Strict<{
    request_id: typeof RequestId;
    code: typeof TeamRefusal;
    detail: Opt<typeof InvalidDefinition>;
  }>,
  typeof OPERATOR_REFUSED_RULE
> = withRule(
  z.strictObject({
    request_id: RequestId,
    code: TeamRefusal,
    detail: InvalidDefinition.describe(
      "Present exactly when code is invalid_definition, so a replayed request returns it.",
    ).optional(),
  }),
  OPERATOR_REFUSED_RULE,
);
export const OperatorRefused: EventDef<
  "operator_refused",
  typeof OperatorRefusedData,
  true
> = event({
  type: "operator_refused",
  critical: true,
  description:
    "An operator request refused under the team-log writer. busy is never logged: no writer was acquired.",
  data: OperatorRefusedData,
});

const POLICY_OPS = ["start", "send", "ask", "monitor", "cancel"] as const;
const DECISIONS = ["allow", "deny"] as const;
const POLICY_SOURCES = ["team", "message_policy", "default"] as const;
const POLICY_DECIDED_RULE = {
  allOf: [
    { oneOf: [{ required: ["call_id"] }, { required: ["request_id"] }] },
    {
      if: { properties: { source: { const: "message_policy" } } },
      then: { required: ["rule"] },
      else: { not: { required: ["rule"] } },
    },
  ],
} as const;
export const MessagePolicyDecidedData: Ruled<
  Strict<{
    op: EnumOf<typeof POLICY_OPS>;
    decision: EnumOf<typeof DECISIONS>;
    source: EnumOf<typeof POLICY_SOURCES>;
    rule: Opt<Strict<{ from: typeof Name; to: typeof Name }>>;
    target: typeof NonEmpty;
    call_id: Opt<typeof CallId>;
    request_id: Opt<typeof RequestId>;
  }>,
  typeof POLICY_DECIDED_RULE
> = withRule(
  z.strictObject({
    op: z.enum(POLICY_OPS).describe("wait is decided as monitor."),
    decision: z.enum(DECISIONS),
    source: z
      .enum(POLICY_SOURCES)
      .describe(
        "team: what the team grants (members send, ask, wait and monitor one another; the lead starts; a starter cancels). message_policy: a rule. default: default deny.",
      ),
    rule: z
      .strictObject({ from: Name, to: Name })
      .describe(
        "message_policy: the matching rule, by its from and to agents (unique per host), so a reordered policy still names it.",
      )
      .optional(),
    target: NonEmpty.describe("The agent (start) or member name."),
    call_id: CallId.describe("A model call's request key.").optional(),
    request_id: RequestId.describe("An operator request's key.").optional(),
  }),
  POLICY_DECIDED_RULE,
);
export const MessagePolicyDecided: EventDef<
  "message_policy_decided",
  typeof MessagePolicyDecidedData,
  true
> = event({
  type: "message_policy_decided",
  critical: true,
  description:
    "Every team authorization decision, for model and operator alike, in the deciding writer's log.",
  data: MessagePolicyDecidedData,
});
