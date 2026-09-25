import { z } from "zod";
import { type EventDef, event } from "../envelope";
import { BranchId } from "../ids";
import { Int, PosInt } from "../primitives";
import { type Ruled, withRule } from "../rules";
import { MemberRef } from "../team";
import type { EnumOf, Strict } from "../zod-types";

// Supervision of host members (Teams Phase 2, spec/schema/README.md, semantic rule 51): the host
// team log's one decision on each ended generation.

const RESTART_POLICIES = ["on_failure", "never"] as const;
const WINDOW_MS_RULE = { allOf: [{ minimum: 1000 }] } as const;
const WindowMs: Ruled<typeof Int, typeof WINDOW_MS_RULE> = withRule(
  Int,
  WINDOW_MS_RULE,
  {
    id: "RestartWindowMs",
    description: "A restart window: at least 1000 ms.",
  },
);
const SUPERVISOR_ACTIONS = ["restart", "stop"] as const;
export const SupervisorDecidedData: Strict<{
  member: typeof MemberRef;
  ended: Strict<{ branch_id: typeof BranchId; seq: typeof PosInt }>;
  action: EnumOf<typeof SUPERVISOR_ACTIONS>;
  restarts_in_window: typeof Int;
  policy: Strict<{
    restart: EnumOf<typeof RESTART_POLICIES>;
    max_restarts: typeof Int;
    within_ms: typeof WindowMs;
  }>;
}> = z.strictObject({
  member: MemberRef.describe("The ended host member generation."),
  ended: z
    .strictObject({ branch_id: BranchId, seq: PosInt })
    .describe("Its member_ended, in its own log."),
  action: z
    .enum(SUPERVISOR_ACTIONS)
    .describe(
      "restart: a member_started of the next generation follows in this append. stop: the name stays ended until an operator starts it.",
    ),
  restarts_in_window: Int.describe(
    "The team log's earlier restart decisions for this name within policy.within_ms of this event's time. Recorded, so no reader recounts it.",
  ),
  policy: z
    .strictObject({
      restart: z.enum(RESTART_POLICIES),
      max_restarts: Int,
      within_ms: WindowMs,
    })
    .describe("The host's members option for this name when it decided."),
});
export const SupervisorDecided: EventDef<
  "supervisor_decided",
  typeof SupervisorDecidedData,
  true
> = event({
  type: "supervisor_decided",
  critical: true,
  description:
    "Phase 2: the host team log's one decision on an ended host member generation (semantic rule 51).",
  data: SupervisorDecidedData,
});
