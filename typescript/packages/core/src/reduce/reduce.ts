import type { EffectStatus, Fold, ParkAddress } from "../fold/state";
import type { KnownEvent } from "../log";
import type { Chain } from "../verify/chain";
import { type TranscriptEntry, transcript } from "./transcript";

export type BranchStatus =
  | "inspection_only"
  | "cancelled"
  | "parked"
  | "in_turn"
  | "idle";

/** The normative v1 projection both languages compare (spec/conformance/README.md). */
export type ReducedState = {
  readonly thread_id: string;
  readonly branch_id: string;
  readonly epoch: number;
  readonly turns_completed: number;
  readonly pending_calls: readonly string[];
  readonly effects: readonly {
    readonly effect_key: string;
    readonly call_id: string;
    readonly status: EffectStatus;
  }[];
  readonly parked: readonly ParkAddress[];
  readonly fork_points: readonly {
    readonly seq: number;
    readonly snapshot_event_id: string;
  }[];
  readonly usage: {
    readonly input_tokens: number;
    readonly output_tokens: number;
    readonly unknown_responses: number;
  };
  readonly transcript: readonly TranscriptEntry[];
  readonly status: BranchStatus;
  readonly head: { readonly seq: number; readonly hash: string };
};

/** The resolved chain's known events; unknown non-critical events are skipped. */
export function knownEvents(chain: Chain): readonly KnownEvent[] {
  return chain.events.flatMap((line) =>
    line.kind === "event" ? [line.event] : [],
  );
}

/** State is reduce(log). `now` is the injected clock that decides snapshot expiry. */
export function reduce(chain: Chain, now: number): ReducedState {
  const { fold } = chain;
  const leaf = chain.segments.at(-1);
  if (leaf === undefined) throw new Error("reduce needs a verified chain");
  return {
    thread_id: leaf.header.thread_id,
    branch_id: leaf.header.branch_id,
    epoch: fold.epoch,
    turns_completed: fold.turns,
    pending_calls: [...fold.pending],
    effects: [...fold.effects].map(([key, effect]) => ({
      effect_key: key,
      call_id: effect.callId,
      status: effect.status,
    })),
    parked: [...fold.parked],
    fork_points: fold.snapshots
      .filter((s) => s.quiescent && (s.expiresAt === null || s.expiresAt > now))
      .map((s) => ({ seq: s.seq, snapshot_event_id: s.eventId })),
    usage: {
      input_tokens: fold.usage.input,
      output_tokens: fold.usage.output,
      unknown_responses: fold.usage.unknown,
    },
    transcript: transcript(knownEvents(chain)),
    status: status(fold),
    head: {
      seq: fold.seq,
      hash: chain.events.at(-1)?.hash ?? leaf.hash,
    },
  };
}

function status(fold: Fold): BranchStatus {
  if (fold.repair) return "inspection_only";
  if (fold.cancelled) return "cancelled";
  if (fold.parked.length > 0) return "parked";
  return fold.turnOpen ? "in_turn" : "idle";
}
