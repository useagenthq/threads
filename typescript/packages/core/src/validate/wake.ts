import type { EventOf, Fold } from "../fold/state";
import type { Run } from "../fold/wake";
import { principalKey } from "../log";
import { invalid, type Violation } from "./violation";

// Rules 32 and 45 for woken (spec/schema/README.md, "Semantic rules"): a wake opens a turn for
// background children's late results recorded in the same append, all of one run, and it acts
// as that run's principal.

/** The run every cause's child was spawned by, or why there is none. */
function causesRun(fold: Fold, causes: readonly string[]): Run | string {
  const { trailingLate, spawnRuns } = fold.wake;
  if (new Set(causes).size !== causes.length)
    return "woken names a cause twice";
  if (causes.length !== trailingLate.size)
    return "woken must name exactly the late results of its own append";
  const runs = causes.map((cause) => {
    const call = trailingLate.get(cause);
    return call === undefined ? undefined : spawnRuns.get(call);
  });
  const [first] = runs;
  if (first === undefined || runs.some((r) => r === undefined))
    return "a woken cause is not the late result of a background child in this append";
  const one = runs.every(
    (r) => r?.principal === first.principal && r.root === first.root,
  );
  return one ? first : "woken names children of more than one run";
}

export function checkWoken(fold: Fold, e: EventOf<"woken">): Violation {
  if (fold.turnOpen) return invalid("woken while a turn is open");
  if (fold.cancelled) return invalid("woken after the thread was cancelled");
  const run = causesRun(fold, e.data.causes);
  if (typeof run === "string") return invalid(run);
  return principalKey(e.actor.principal) === run.principal
    ? undefined
    : invalid(
        "woken acts for another principal than the run that spawned its children",
      );
}
