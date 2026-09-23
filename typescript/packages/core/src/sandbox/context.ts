import { BranchId } from "../log";
import { err, ok } from "../result";
import type { ResourceLedger } from "../store/ledger";
import type { Writer } from "../store/writer";
import type { Failure, SandboxContext, Stale } from "./protocol";

// SandboxContext (spec/api.json): what every provider operation carries so the adapter can
// fence at its real dispatch point. One hook, two authorities.

/** The owner's writer: passes only while it still holds the branch lease at its epoch. */
export function ownerContext(writer: Writer): SandboxContext {
  return {
    authority: {
      kind: "owner",
      branch_id: BranchId.parse(writer.lease.branchId),
      epoch: writer.lease.epoch,
    },
    fence: async () => {
      const live = writer.fence();
      return live.ok
        ? ok(undefined)
        : err({ code: "stale_epoch", message: live.error.message });
    },
  };
}

/** gc's claim on one row: passes only while the row still carries it and is collectable. */
export function cleanupContext(
  ledger: ResourceLedger,
  resourceId: string,
  claim: string,
): SandboxContext {
  return {
    authority: { kind: "cleanup", resource_id: resourceId, claim },
    fence: async () => {
      const held = ledger.claimed(resourceId, claim);
      return held.ok
        ? ok(undefined)
        : err({ code: "cleanup_claim_lost", message: held.error.message });
    },
  };
}

/** A fence refusal: the dispatch never reached the provider, and the caller lost its authority. */
export function isRefusal<C extends string>(
  failure: Failure<C> | Stale,
): failure is Stale {
  return (
    failure.code === "stale_epoch" || failure.code === "cleanup_claim_lost"
  );
}
