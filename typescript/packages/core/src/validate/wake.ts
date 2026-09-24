import type { EventOf, Fold } from "../fold/state";
import { spawnRuns, wakeBar } from "../fold/wake";
import { principalKey } from "../log";
import { invalid, type Violation } from "./violation";

// Rules 32 and 45 for woken (spec/schema/README.md, "Semantic rules"): a wake opens a turn for
// background children's late results recorded in the same append, all of one run that may still
// be woken (fold/wake.ts wakeBar), and it acts as that run's principal.

/** The background calls of the causes, or why they aren't this append's late-result tail. */
function causeCalls(fold: Fold, causes: readonly string[]): string[] | string {
  const { trailingLate } = fold.wake;
  if (new Set(causes).size !== causes.length)
    return "woken names a cause twice";
  // The causes are the tail of the late-result block: a late result before the first cause
  // was recorded without a wake (an older writer, or a result held for another run).
  const block = [...trailingLate.keys()];
  const tail = block.slice(Math.min(...causes.map((c) => block.indexOf(c))));
  if (causes.some((c) => !trailingLate.has(c)) || tail.length !== causes.length)
    return "woken must name every late result of its own append, and only those";
  return causes.map((c) => trailingLate.get(c) ?? "");
}

export function checkWoken(fold: Fold, e: EventOf<"woken">): Violation {
  if (fold.turnOpen) return invalid("woken while a turn is open");
  const calls = causeCalls(fold, e.data.causes);
  if (typeof calls === "string") return invalid(calls);
  const barred = wakeBar(fold, calls);
  if (barred !== undefined) return invalid(barred);
  const [run] = spawnRuns(fold, calls);
  return run?.principal === principalKey(e.actor.principal)
    ? undefined
    : invalid(
        "woken acts for another principal than the run that spawned its children",
      );
}
