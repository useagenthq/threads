import { apply } from "../fold/apply";
import { type EventLine, emptyFold } from "../fold/state";

// The team index (spec/schema/README.md, "Teams", the replay rule): every row of store.sql's
// team tables is inserted or changed by the append that holds its bytes. insertRows is what a
// sender's or starter's append inserts, changeRows what each recipient's and member's own
// events change. A writer calls both for each append (insert first); a rebuild calls insertRows
// for every log, then changeRows for every log, so it doesn't depend on the order logs are read.
// Ported from spec/tools/fixtures/team_index.py.

export { changeRows } from "./change";
export { insertRows } from "./insert";
export type { TeamLog } from "./scope";

/** The event ids at which the reducer opens a turn, re-folded from the log's lines. */
export function turnOpeners(lines: readonly EventLine[]): ReadonlySet<string> {
  const fold = emptyFold();
  const opened = new Set<string>();
  for (const line of lines) {
    const before = fold.turnOpen;
    apply(fold, line);
    if (!before && fold.turnOpen) opened.add(line.event.event_id);
  }
  return opened;
}
