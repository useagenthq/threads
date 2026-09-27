import { derivedId } from "threadsai/adapter";
import { PROVENANCE } from "../protocol";

// The ids an outbound call derives rather than generates, and the provenance claim it may carry.
// Nothing here is random: a crash and a re-dispatch must arrive at the same messageId, or a peer
// that deduplicates would see two messages (30-a2a decision H30-1).

const MESSAGE = "threads/a2a-message";
const CONTEXT = "threads/a2a-context";
const REQUEST = "threads/a2a-request";

/**
 * From `(branch_id, call_id)` **only**, never from the attempt: every attempt of one call carries
 * this same id, which is what makes a peer's deduplication see one message.
 */
export function messageIdOf(branchId: string, callId: string): string {
  return derivedId(MESSAGE, [branchId, callId]);
}

/**
 * One A2A conversation per thread and remote. A tool never sends a context the model chose: a
 * model that could name a context could reach another conversation with the same partner.
 */
export function contextIdOf(threadId: string, remote: string): string {
  return derivedId(CONTEXT, [threadId, remote]);
}

/** `provenance: "opaque"`: linkable across the calls of one request, and anonymous. */
export type Claim = {
  /** sha256 of (tenant, root request), truncated: the partner can group, not identify. */
  readonly request: string;
  readonly hops: number;
};

/**
 * The claim an opaque call carries. `root` is the request id this thread itself arrived under when
 * it came in over A2A, so one request's calls share an id across hops; otherwise the thread id.
 * **No principal, tenant or subject is ever sent** — only a digest that includes the tenant.
 */
export function claimOf(
  tenant: string,
  root: string,
  hops: number,
): Record<string, Claim> {
  return {
    [PROVENANCE]: { request: requestIdOf(tenant, root), hops },
  };
}

/** The digest's first 16 bytes as hex: enough to group one request's calls, too little to undo. */
export function requestIdOf(tenant: string, root: string): string {
  return derivedId(REQUEST, [tenant, root]).replaceAll("-", "");
}
