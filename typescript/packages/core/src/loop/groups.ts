import type { ToolSpec } from "../log";

// Parallel tool calls (spec/schema/README.md): which pending calls may run together. Pinned by
// spec/conformance/vectors/tool-groups.json.

/** What the grouping rule reads about one pending call. */
export type Candidate = {
  /** The bound tool is declared `concurrent: true`. */
  readonly concurrent: boolean;
  /** The pinned effect class. */
  readonly effectClass: ToolSpec["effect_class"];
  readonly framework: boolean;
  readonly endsTurn: boolean;
  /** The recorded authorization; none: not authorized yet. */
  readonly decision: "allow" | "ask" | "deny" | "none";
};

function joins(c: Candidate): boolean {
  return (
    c.concurrent &&
    c.effectClass === "read_only" &&
    !c.framework &&
    !c.endsTurn &&
    c.decision === "allow"
  );
}

/**
 * The dispatch plan, as call indexes: each group is the longest run of consecutive calls that
 * may run together; every other call is a group of one and runs alone.
 */
export function groups(
  pending: readonly Candidate[],
): readonly (readonly number[])[] {
  const plan: number[][] = [];
  for (const [i, c] of pending.entries()) {
    const last = plan.at(-1);
    const head = last?.[0];
    const prior = head === undefined ? undefined : pending[head];
    if (last !== undefined && prior !== undefined && joins(prior) && joins(c))
      last.push(i);
    else plan.push([i]);
  }
  return plan;
}
