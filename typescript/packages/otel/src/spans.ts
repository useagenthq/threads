import type { ChainEvent } from "@threads/core/internal/feed";
import type { Span } from "./span";
import type { ParentOf } from "./turns";
import { type Walked, walk } from "./walk";

// spans(branch, lookup): a pure function of the log (spec/otel/README.md). The same chain and the
// same other chains always give the same spans, ids and attributes.

/** Another branch's resolved chain (a parent's), or undefined when it is missing or unreadable. */
export type Lookup = (branchId: string) => readonly ChainEvent[] | undefined;

export type Branch = {
  readonly tenant: string;
  readonly branchId: string;
  /** The branch's resolved chain: a fork's includes its parent's prefix. */
  readonly chain: readonly ChainEvent[];
  readonly content: boolean;
};

/** The spans that close in the branch's own segment, in close order. */
export function spans(branch: Branch, lookup: Lookup): readonly Span[] {
  const walked = walk({ ...branch, parentOf: parents(lookup, branch) });
  const forkAt = branch.chain.reduce(
    (at, line) =>
      line.event.branch_id === branch.branchId
        ? at
        : Math.max(at, line.event.seq),
    0,
  );
  return walked.spans.filter((s) => s.closeSeq > forkAt);
}

/** Resolves a child's parent span through the parent's own walk, each chain walked once. */
function parents(lookup: Lookup, branch: Branch): ParentOf {
  const walked = new Map<string, Walked | undefined>();
  const of: ParentOf = (parent) => {
    if (!walked.has(parent.branch_id)) {
      const chain = lookup(parent.branch_id);
      walked.set(
        parent.branch_id,
        chain === undefined
          ? undefined
          : walk({
              ...branch,
              branchId: parent.branch_id,
              chain,
              parentOf: of,
            }),
      );
    }
    return walked.get(parent.branch_id)?.anchors.get(parent.event_id);
  };
  return of;
}
