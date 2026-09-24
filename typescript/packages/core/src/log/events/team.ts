import { z } from "zod";
import { ActorWithPrincipal, Budget } from "../common";
import { type EventDef, event, eventWithActor } from "../envelope";
import { BranchId, EventId, MonitorId, TeamId, ThreadId, WaitId } from "../ids";
import { NonEmpty, PosInt, Sha256, TimeMs } from "../primitives";
import {
  CompletedResult,
  EndedResult,
  MemberRef,
  Provenance,
  StoredMemberResult,
} from "../team";
import type { Arr, EnumOf, Lit, Opt, Strict } from "../zod-types";
import { ParkReason } from "./control";

// A team's lifecycle, waits, monitors and wakes (spec/schema/README.md, "Teams"). Each event
// holds every byte of the index rows it writes, so a fold over the team's logs rebuilds them.

export const TeamOpenedData: Strict<{
  team: typeof TeamId;
  lead: typeof MemberRef;
  lead_thread_id: typeof ThreadId;
}> = z.strictObject({
  team: TeamId,
  lead: MemberRef,
  lead_thread_id: ThreadId,
});
export const TeamOpened: EventDef<"team_opened", typeof TeamOpenedData, true> =
  event({
    type: "team_opened",
    critical: true,
    description:
      "The team log's first event, in place of thread_started: opened in the lead's first append. The team log never renders to a model, never parks and never opens a turn.",
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
export const MemberStartedData: Strict<{
  member: typeof MemberRef;
  agent: typeof NonEmpty;
  config_hash: typeof Sha256;
  thread_id: typeof ThreadId;
  parent: typeof TeamMemberParent;
  provenance: typeof Provenance;
  budget: Opt<typeof Budget>;
}> = z.strictObject({
  member: MemberRef,
  agent: NonEmpty,
  config_hash: Sha256.describe(
    "The pinned definition, whose canonical bytes are stored before this append; materialize rebinds against it.",
  ),
  thread_id: ThreadId.describe(
    "The member's thread, opened at materialize (the starting window has no branch).",
  ),
  parent: TeamMemberParent.describe(
    "The structural parent the member's thread_started will carry: the lead's member_started, or the lead's thread_started for an operator start.",
  ),
  provenance: Provenance,
  budget: Budget.describe(
    "The member's own budget: the minimum of start's budget and the matching messagePolicy rule's.",
  ).optional(),
});
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

export const MemberIdleData: Strict<{ result: typeof CompletedResult }> =
  z.strictObject({ result: CompletedResult });
export const MemberIdle: EventDef<"member_idle", typeof MemberIdleData, true> =
  event({
    type: "member_idle",
    critical: true,
    description:
      "In the member's log, with the turn_completed that ends its task: the row becomes idle with this result, and each settle monitor and an unfired task monitor fires in the same append.",
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
