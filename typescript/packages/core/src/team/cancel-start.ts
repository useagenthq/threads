import type { MailEnvelope } from "../log";
import type { Batch } from "./batch";
import { received } from "./mail";
import { type AppendContext, settle } from "./settle";

// A cancel for a starting member (spec/schema/README.md, "Teams", "A cancel for a starting
// member"): its materialize never tries the rebind, so a member whose setup can't succeed can
// still be cancelled. After the branch's thread_started and task input, the same append takes the
// cancel, closes the task turn as the cancellation step does, and ends the member cancelled.
// Reference: spec/tools/fixtures/ops_start.py (materialize, _cancelled).

const HOST = {
  type_version: 1,
  critical: true,
  actor: { kind: "host" },
} as const;

/** The cancel's receipt, the barrier, the turn's cancelled end, and member_ended{cancelled}. */
export async function startCancelled(
  ctx: AppendContext & { readonly batch: Batch },
  task: MailEnvelope,
  cancel: MailEnvelope,
): Promise<void> {
  ctx.batch.add(received(cancel));
  const barrier = ctx.batch.add({
    type: "cancel_requested",
    type_version: 1,
    critical: true,
    actor: { kind: "host", principal: cancel.provenance.principal },
    data: { scope: "tree" },
  });
  ctx.batch.add({
    ...HOST,
    type: "cancelled",
    data: { request_event_id: barrier },
  });
  ctx.batch.add({
    ...HOST,
    type: "turn_completed",
    data: { reason: "cancelled" },
  });
  await settle(
    {
      ...ctx,
      provenance: task.provenance,
      put: async () => {
        throw new Error("a cancelled start's result has no text");
      },
    },
    { status: "cancelled" },
  );
}
