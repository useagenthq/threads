import type { Chain } from "../verify";
import { turnProvenance } from "./provenance";
import { type AppendContext, settle } from "./settle";

// A failed rebind when the worker resumes a member (spec/schema/README.md, "Teams", A failed
// rebind): the definition a parked, stranded or woken member was started with is gone or changed
// in this process. Its end is one append under its writer: each pending call whose effect never
// began closes not_executed, an open turn closes {error, code}, and the member ends failed as any
// end does. A call whose effect is in doubt keeps its record: it is never dispatched again.

export type RebindCode = "pin_unavailable" | "pin_mismatch";

export function rebindFailed(
  ctx: AppendContext & { readonly chain: Chain },
  code: RebindCode,
): void {
  const { fold } = ctx.chain;
  const provenance = turnProvenance(ctx.db, ctx.chain);
  if (provenance === undefined) throw new Error("a member's log has a turn");
  const inDoubt = new Set(
    [...fold.effects.values()]
      .filter((e) => e.status === "begun" || e.status === "unknown")
      .map((e) => e.callId),
  );
  for (const callId of fold.pending) {
    if (inDoubt.has(callId)) continue;
    ctx.batch.add({
      type: "tool_result",
      type_version: 1,
      critical: true,
      actor: { kind: "host" },
      data: {
        call_id: callId,
        is_error: true,
        completeness: "complete",
        origin: "not_executed",
        preview: `not executed: rebind failed: ${code}`,
      },
    });
  }
  if (fold.turnOpen)
    ctx.batch.add({
      type: "turn_completed",
      type_version: 1,
      critical: true,
      actor: { kind: "host" },
      data: { reason: "error", code },
    });
  settle(
    {
      ...ctx,
      provenance,
      put: () => {
        throw new Error("a failed rebind's result has no text");
      },
    },
    { status: "failed", error: { code, message: `rebind failed: ${code}` } },
  );
}
