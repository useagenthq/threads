import type { ChannelAdapter } from "@threads/core";
import type {
  ArtifactStore,
  BranchId,
  KnownEvent,
  VerifiedLog,
  Writer,
} from "@threads/core/host";
import { sendOp } from "./deliver";

// The host's own sends whose source is no longer due (a question answered or expired, a card
// decided) but whose call is still open, after a crash (spec/schema/README.md, "Channel
// replies"). Only this path settles them: the agent's loop never recovers a host call. A send
// that may have gone out is reconciled like any other; one that never began is closed, unsent.

type Fold = VerifiedLog["fold"];

export type Leftover = {
  readonly callId: string;
  readonly op: Parameters<ChannelAdapter["perform"]>[0];
  readonly requestId: string;
  /** Begun or unknown: it may have been sent. */
  readonly inDoubt: boolean;
};

/** Open host calls other than the derived ones; a send parked for a human is left to them. */
export function leftovers(
  branch: BranchId,
  fold: Fold,
  events: readonly KnownEvent[],
  derived: ReadonlySet<string>,
): readonly Leftover[] {
  return [...fold.hostCalls].flatMap((callId) => {
    if (derived.has(callId) || fold.calls.get(callId)?.result !== undefined)
      return [];
    const key = `${branch}:${callId}`;
    if (fold.parked.some((a) => a.kind === "effect" && a.id === key)) return [];
    const call = events.find(
      (e) => e.type === "tool_call" && e.data.call_id === callId,
    );
    if (call?.type !== "tool_call") return [];
    const status = fold.effects.get(key)?.status;
    return [
      {
        callId,
        op: call.data.input,
        requestId: call.data.request_event_id,
        inDoubt: status === "begun" || status === "unknown",
      },
    ];
  });
}

/** Reconciles a leftover that may have been sent; closes one that never was, unsent. */
export async function settleLeftover(
  adapter: ChannelAdapter,
  writer: Writer,
  artifacts: ArtifactStore,
  left: Leftover,
  stopping: AbortSignal,
): Promise<void> {
  if (left.inDoubt) {
    await sendOp(adapter, writer, artifacts, left, stopping);
    return;
  }
  // A refused append means this writer lost its lease: the next holder closes the leftover.
  await writer.append([
    {
      type: "tool_result",
      type_version: 1,
      critical: true,
      actor: { kind: "host" },
      data: {
        call_id: left.callId,
        is_error: true,
        origin: "not_executed",
        completeness: "complete",
        preview: "not sent: no longer due",
      },
    },
  ]);
}
