import { z } from "zod";
import { ActorWithPrincipal, Budget } from "../common";
import { type EventDef, event, eventWithActor } from "../envelope";
import { BranchId, EventId, MonitorId, TeamId, ThreadId, WaitId } from "../ids";
import { Name, NonEmpty, PosInt, Sha256, TimeMs } from "../primitives";
import { type Ruled, withRule } from "../rules";
import {
  CompletedResult,
  EndedResult,
  MemberRef,
  Provenance,
  StoredMemberResult,
  TurnFailure,
} from "../team";
import type { Arr, EnumOf, Lit, Opt, Strict } from "../zod-types";
import { ParkReason } from "./control";

// A team's lifecycle, waits, monitors and wakes (spec/schema/README.md, "Teams"). Each event
// holds every byte of the index rows it writes, so a fold over the team's logs rebuilds them.

const TEAM_KINDS = ["lead", "host"] as const;
const TEAM_OPENED_RULE = {
  if: { properties: { kind: { const: "host" } }, required: ["kind"] },
  then: {
    required: ["tenant"],
    not: { anyOf: [{ required: ["lead"] }, { required: ["lead_thread_id"] }] },
  },
  else: { required: ["lead", "lead_thread_id"], not: { required: ["tenant"] } },
} as const;
export const TeamOpenedData: Ruled<
  Strict<{
    team: typeof TeamId;
    kind: Opt<EnumOf<typeof TEAM_KINDS>>;
    lead: Opt<typeof MemberRef>;
    lead_thread_id: Opt<typeof ThreadId>;
    tenant: Opt<typeof NonEmpty>;
  }>,
  typeof TEAM_OPENED_RULE
> = withRule(
  z.strictObject({
    team: TeamId,
    kind: z
      .enum(TEAM_KINDS)
      .describe(
        "Absent or lead: a lead's team, with lead and lead_thread_id. host (Phase 2): a tenant's host team, which has no lead, holds the tenant's host members and never closes.",
      )
      .optional(),
    lead: MemberRef.optional(),
    lead_thread_id: ThreadId.optional(),
    tenant: NonEmpty.describe(
      "host only: the tenant whose host team this is (its teams row's tenant_id).",
    ).optional(),
  }),
  TEAM_OPENED_RULE,
);
export const TeamOpened: EventDef<"team_opened", typeof TeamOpenedData, true> =
  event({
    type: "team_opened",
    critical: true,
    description:
      "The team log's first event, in place of thread_started: opened in the lead's first append, or for a host team by the first append that addresses one of its host members. The team log never renders to a model, never parks and never opens a turn.",
    data: TeamOpenedData,
  });

// A member's structural parent: always through the lead (spec/schema/README.md, "Teams").
const TeamMemberParent: Strict<{
  thread_id: typeof ThreadId;
  branch_id: typeof BranchId;
  event_id: typeof EventId;
  relation: Lit<"team_member">;
}> = z.strictObject({
  thread_id: ThreadId,
  branch_id: BranchId,
  event_id: EventId,
  relation: z.literal("team_member"),
});
/** What a lead (or the operator) chose for a member of a dynamic agent (lane 26). */
export const MemberDefine: Strict<{
  instructions: Opt<typeof NonEmpty>;
  tools: Arr<typeof Name>;
  model: typeof NonEmpty;
}> = z
  .strictObject({
    instructions: NonEmpty.describe(
      "The written instructions, byte for byte: the member's line 0 ends with them in the delimited block. Absent: the preamble alone.",
    ).optional(),
    tools: z
      .array(Name)
      .describe(
        "The chosen tools, unique, in the template's pinned order; none of the framework set. The member pins exactly these and the framework tools.",
      ),
    model: NonEmpty.describe("The chosen key of the template's models."),
  })
  .meta({ id: "MemberDefine" });

const DEFINITION_FIELDS = ["label", "instructions", "tools", "model"] as const;
const DEFINITION_REASONS = ["not_allowed", "invalid"] as const;
export const InvalidDefinition: Strict<{
  field: EnumOf<typeof DEFINITION_FIELDS>;
  reason: EnumOf<typeof DEFINITION_REASONS>;
  allowed: Opt<Arr<z.ZodString>>;
}> = z
  .strictObject({
    field: z.enum(DEFINITION_FIELDS),
    reason: z.enum(DEFINITION_REASONS),
    allowed: z
      .array(z.string())
      .describe(
        "With not_allowed on tools or model: the names the template allows.",
      )
      .optional(),
  })
  .meta({
    id: "InvalidDefinition",
    description:
      "Why a start's chosen fields (label, instructions, tools, model) were refused: its invalid_definition detail.",
  });

const MEMBER_STARTED_RULE = {
  if: { required: ["host_member"] },
  then: {
    not: {
      anyOf: [
        { required: ["parent"] },
        { required: ["define"] },
        { required: ["label"] },
        { required: ["budget"] },
      ],
    },
  },
  else: {
    required: ["parent", "provenance"],
    not: { required: ["restart_of"] },
  },
} as const;
export const MemberStartedData: Ruled<
  Strict<{
    member: typeof MemberRef;
    agent: typeof NonEmpty;
    config_hash: typeof Sha256;
    thread_id: typeof ThreadId;
    parent: Opt<typeof TeamMemberParent>;
    provenance: Opt<typeof Provenance>;
    budget: Opt<typeof Budget>;
    define: Opt<typeof MemberDefine>;
    label: Opt<typeof NonEmpty>;
    host_member: Opt<Lit<true>>;
    restart_of: Opt<typeof PosInt>;
  }>,
  typeof MEMBER_STARTED_RULE
> = withRule(
  z.strictObject({
    member: MemberRef,
    agent: NonEmpty,
    config_hash: Sha256.describe(
      "The pinned definition, whose canonical bytes are stored before this append; materialize rebinds against it.",
    ),
    thread_id: ThreadId.describe(
      "The member's thread, opened at materialize (the starting window has no branch).",
    ),
    parent: TeamMemberParent.describe(
      "The structural parent the member's thread_started will carry: the lead's member_started, or the lead's thread_started for an operator start. Absent exactly for a host member, a root thread.",
    ).optional(),
    provenance: Provenance.describe(
      "Required unless host_member. A host member's start has one only when an operator restarts it.",
    ).optional(),
    budget: Budget.describe(
      "The member's own budget: the minimum of start's budget and the matching messagePolicy rule's.",
    ).optional(),
    define: MemberDefine.describe(
      "Present exactly when the agent is a dynamic agent: what its starter chose. config_hash binds it.",
    ).optional(),
    label: NonEmpty.describe(
      "A display name the starter gave (1-64 code points, no control or format characters; semantic rule 46). Never hashed, never rendered to a model.",
    ).optional(),
    host_member: z
      .literal(true)
      .describe(
        "Phase 2: a host member, in a host team's log, named by its agent. No task mail and no task monitor go with it; it has no parent and its thread is a root.",
      )
      .optional(),
    restart_of: PosInt.describe(
      "host_member only: the generation this start replaces (semantic rule 51).",
    ).optional(),
  }),
  MEMBER_STARTED_RULE,
);
export const MemberStarted: EventDef<
  "member_started",
  typeof MemberStartedData,
  true
> = event({
  type: "member_started",
  critical: true,
  description:
    "In the starter's log (the lead, or the team log for an operator start), with the task's message_sent: inserts the member's row (starting), its task mail and the starter's task monitor.",
  data: MemberStartedData,
});

const MEMBER_IDLE_RULE = {
  oneOf: [{ required: ["result"] }, { required: ["turn_failed"] }],
} as const;
export const MemberIdleData: Ruled<
  Strict<{
    result: Opt<typeof CompletedResult>;
    turn_failed: Opt<typeof TurnFailure>;
  }>,
  typeof MEMBER_IDLE_RULE
> = withRule(
  z.strictObject({
    result: CompletedResult.optional(),
    turn_failed: TurnFailure.describe(
      "Phase 2, a host member only: its turn failed and only that turn ended. The row goes back to idle and keeps its last result.",
    ).optional(),
  }),
  MEMBER_IDLE_RULE,
);
export const MemberIdle: EventDef<"member_idle", typeof MemberIdleData, true> =
  event({
    type: "member_idle",
    critical: true,
    description:
      "In the member's log, with the turn_completed that ends its task: the row becomes idle with this result, and each settle monitor and an unfired task monitor fires in the same append. A host member's failed turn records turn_failed instead, and fires nothing.",
    data: MemberIdleData,
  });

export const MemberEndedData: Strict<{ result: typeof EndedResult }> =
  z.strictObject({ result: EndedResult });
export const MemberEnded: EventDef<
  "member_ended",
  typeof MemberEndedData,
  true
> = event({
  type: "member_ended",
  critical: true,
  description:
    "The member's terminal result (failed, cancelled, budget_exhausted, or a lead's handed_off). Its append fires every monitor on this generation and refuses every pending inbound mail with a bounce. A lead's also closes the team and cancels every live member.",
  data: MemberEndedData,
});

export const MemberObservedData: Strict<{
  monitor_id: typeof MonitorId;
  result: typeof StoredMemberResult;
  source: Strict<{
    thread_id: typeof ThreadId;
    branch_id: typeof BranchId;
    seq: typeof PosInt;
  }>;
}> = z.strictObject({
  monitor_id: MonitorId,
  result: StoredMemberResult.describe(
    "Byte for byte the target's committed member_idle or member_ended result at source.",
  ),
  source: z.strictObject({
    thread_id: ThreadId,
    branch_id: BranchId,
    seq: PosInt,
  }),
});
export const MemberObserved: EventDef<
  "member_observed",
  typeof MemberObservedData,
  true
> = event({
  type: "member_observed",
  critical: true,
  description:
    "The waiter or monitor found its target already settled when it registered: recorded in the waiter's own log, with no mail and no monitor row.",
  data: MemberObservedData,
});

export const MonitorSetData: Strict<{ member: typeof MemberRef }> =
  z.strictObject({ member: MemberRef });
export const MonitorSet: EventDef<"monitor_set", typeof MonitorSetData, true> =
  event({
    type: "monitor_set",
    critical: true,
    description:
      "A model monitor on a member: an end monitor whose id is <branch_id>:<this event_id>:<member name>. Its member_ended mail opens a turn for an idle watcher.",
    data: MonitorSetData,
  });

const WAIT_MODES = ["all", "any"] as const;
export const WaitStartedData: Strict<{
  wait_id: typeof WaitId;
  members: Arr<typeof MemberRef>;
  mode: z.ZodXor<readonly [EnumOf<typeof WAIT_MODES>, typeof PosInt]>;
  deadline: typeof TimeMs;
}> = z.strictObject({
  wait_id: WaitId,
  members: z
    .array(MemberRef)
    .min(1)
    .describe(
      "Frozen at the call. Each gets a settle monitor <branch_id>:<this event_id>:<name>, or a member_observed.",
    ),
  mode: z
    .xor([z.enum(WAIT_MODES), PosInt])
    .describe("all, any, or the number of members that must settle."),
  deadline: TimeMs,
});
export const WaitStarted: EventDef<
  "wait_started",
  typeof WaitStartedData,
  true
> = event({
  type: "wait_started",
  critical: true,
  description:
    "A wait on members, in a member's log or the team log. A member waiter also parks {kind: wait, id: wait_id}.",
  data: WaitStartedData,
});

export const WaitFinishedData: Strict<{
  wait_id: typeof WaitId;
  finished: Arr<typeof StoredMemberResult>;
  parked: Arr<Strict<{ member: typeof MemberRef; reason: typeof ParkReason }>>;
  pending: Arr<typeof MemberRef>;
  timed_out: z.ZodBoolean;
}> = z.strictObject({
  wait_id: WaitId,
  finished: z.array(StoredMemberResult),
  parked: z.array(z.strictObject({ member: MemberRef, reason: ParkReason })),
  pending: z.array(MemberRef),
  timed_out: z
    .boolean()
    .describe("true only when the deadline passed with the mode unmet."),
});
export const WaitFinished: EventDef<
  "wait_finished",
  typeof WaitFinishedData,
  true
> = event({
  type: "wait_finished",
  critical: true,
  description:
    "Closes a wait: deletes every remaining settle monitor with this wait_id. Settlement is by commit order, never by timestamps.",
  data: WaitFinishedData,
});

export const WokenData: Strict<{ causes: Arr<typeof EventId> }> =
  z.strictObject({
    causes: z
      .array(EventId)
      .min(1)
      .describe(
        "The tool_result_late events in this same append that woke the thread.",
      ),
  });
export const Woken: EventDef<
  "woken",
  typeof WokenData,
  true,
  typeof ActorWithPrincipal
> = eventWithActor({
  type: "woken",
  critical: true,
  description:
    "Opens a turn for background results that arrived while no turn was open and no cancel barrier stood. Written only in the append that records those results, never retroactively. Renders nothing: the late results render.",
  data: WokenData,
  actor: ActorWithPrincipal,
});

/** The tenant a team_opened indexes its team under: its lead's, or a host team's own. */
export function openedTenant(d: z.infer<typeof TeamOpenedData>): string {
  const tenant = d.lead?.tenant ?? d.tenant;
  if (tenant === undefined)
    throw new Error(
      "team_opened names no tenant: its schema rule was bypassed",
    );
  return tenant;
}
