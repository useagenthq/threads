import { z } from "zod";
import { ArtifactRef, Principal } from "./common";
import { BudgetExceededData } from "./events/control";
import { HAS_TEXT_OR_REF } from "./events/one-of";
import { EventId, MemberName, TeamId, ThreadId } from "./ids";
import { NonEmpty, PosInt } from "./primitives";
import { type Ruled, withRule } from "./rules";
import type { Arr, EnumOf, Lit, Opt, Strict } from "./zod-types";

// Shapes shared by the team events (spec/schema/README.md, "Teams"): member references,
// provenance and results (the mail envelope is mail.ts).

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

// The host API RunErrorCode, then the two rebind codes. Spelled out: isolatedDeclarations
// can't infer a spread.
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
  "setup_failed",
] as const;
export const MemberErrorCode: EnumOf<typeof MEMBER_ERROR_CODES> = z
  .enum(MEMBER_ERROR_CODES)
  .meta({
    id: "MemberErrorCode",
    description:
      "A failed member's code: the host API RunErrorCode, plus pin_unavailable and pin_mismatch for a failed rebind, and setup_failed for a member whose setup kept failing.",
  });

// What a host member's failed turn records, by its turn_completed (spec/schema/README.md, Teams
// Phase 2, "A host member's failed turn"): the reason, or for error its code, else model_error.
const TURN_FAILED_CODES = [
  "model_error",
  "content_unsupported",
  "continuation_unsupported",
  "transport_fence_unsupported",
  "secret_in_provider_output",
  "max_turns",
  "max_output",
  "stop_hook_limit",
  "context_exhausted",
  "output_invalid",
  "input_denied",
  "model_unavailable",
  "budget_exhausted",
  "interrupted",
] as const;
export const TurnFailure: Strict<{
  code: EnumOf<typeof TURN_FAILED_CODES>;
  message: z.ZodString;
}> = z
  .strictObject({
    code: z
      .enum(TURN_FAILED_CODES)
      .describe(
        "Mapped from the failed turn's turn_completed: its reason (max_turns, max_output, stop_hook_limit, context_exhausted, output_invalid, input_denied, model_unavailable, budget_exhausted for the hop cap or the caller's run budget, interrupted for a turn recovery closed), or for reason error its code (content_unsupported, continuation_unsupported, transport_fence_unsupported, secret_in_provider_output), else model_error.",
      ),
    message: z.string(),
  })
  .meta({
    id: "TurnFailure",
    description:
      "Why a host member's turn failed (Phase 2). Only that turn ends: its unanswered asks close failed and the member goes back to idle.",
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
