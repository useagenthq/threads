import type { KnownEvent, Principal } from "../log";
import type { MemoryOrigin } from "./protocol";

// What the host derives from the recorded log, never from the model's claim (
// and 9): a memory's origin and whether a write counts as the principal's, and the corpus
// revision a forked branch searches as of.

/** Untrusted material: tool output, an untrusted reference, a summary, a channel item. */
function untrusted(e: KnownEvent): boolean {
  return (
    e.type === "tool_result" ||
    e.type === "tool_result_late" ||
    e.type === "compacted" ||
    e.type === "channel_delivery" ||
    (e.type === "injected" && e.data.trust === "untrusted_reference")
  );
}

const same = (a: Principal, b: Principal | undefined): boolean =>
  b !== undefined &&
  a.issuer === b.issuer &&
  a.tenant === b.tenant &&
  a.subject === b.subject;

/**
 * "The user started this turn" is not "the user authored this memory": a write is the
 * principal's only when one principal's input started every turn and nothing untrusted is in
 * context. Conservative: it reads the whole branch, not just what the last request rendered.
 */
export function principalAuthored(events: readonly KnownEvent[]): boolean {
  const inputs = events.flatMap((e) =>
    e.type === "user_input" ? [e.actor.principal] : [],
  );
  const last = inputs.at(-1);
  if (last === undefined || events.some(untrusted)) return false;
  return inputs.every((p) => same(last, p));
}

/** The record's origin, kept forever: user only when the principal authored it. */
export function memoryOrigin(events: readonly KnownEvent[]): MemoryOrigin {
  if (principalAuthored(events)) return "user";
  return events.some(untrusted) ? "tool_output" : "model";
}

/**
 * The revision this branch searches as of: its fork snapshot's under `pinned`; undefined (the
 * live corpus) under `current`, for a root, or for a snapshot taken without knowledge.
 */
export function knowledgeRevision(
  events: readonly KnownEvent[],
): number | undefined {
  const fork = events.findLast((e) => e.type === "fork");
  if (fork?.type !== "fork" || fork.data.knowledge_policy !== "pinned")
    return undefined;
  const at = events.find((e) => e.seq === fork.seq - 1);
  return at?.type === "snapshot" ? at.data.knowledge_revision : undefined;
}
