import { expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { runOpener } from "../../src/fold/openers";
import { EventId, type KnownEvent, ThreadId } from "../../src/log";
import { ownCovering } from "../../src/loop/ledger";
import { knownEvents } from "../../src/reduce";
import { CASES, caseLog, verified } from "./kit";

// Which run a turn is charged to (spec/schema/README.md, "Teams"): a lead's turn woken by a
// member's settlement belongs to the run whose request started that member, even after a later
// run began (21D review M3), not to the latest input's run.

const lead = knownEvents(verified(caseLog("team-settle-wakes-lead", "lead")));
const THREAD = ThreadId.parse("0192a000-0000-7000-8000-0000000000b1");

/** The recorded user_input, with a run budget and, for a later run, a new id. */
function input(id: string | undefined, max: number): KnownEvent {
  const first = lead[1];
  if (first?.type !== "user_input") throw new Error("the lead's second event");
  return {
    ...first,
    event_id: id === undefined ? first.event_id : EventId.parse(id),
    data: { ...first.data, budget: { max_model_requests: max } },
  };
}

test("a lead's wake turn is charged to the run that started the member, not the latest input's", () => {
  const [started, , ...rest] = lead;
  const settled = rest.findIndex((e) => e.type === "message_received");
  const ended = rest
    .slice(0, settled)
    .findLast((e) => e.type === "turn_completed");
  if (started === undefined || ended === undefined)
    throw new Error("the case log");
  const later = "0192e001-0000-7000-8000-0000000000f1";
  const events = [
    started,
    input(undefined, 20),
    ...rest.slice(0, settled),
    input(later, 30),
    ended,
    ...rest.slice(settled),
  ];
  const run = ownCovering({
    threadId: THREAD,
    fold: { policy: undefined },
    events,
  }).filter((c) => c.scope === "run");
  expect(run.map((c) => c.budgetId)).toEqual([
    `run:${THREAD}:${lead[1]?.event_id}`,
  ]);
});

test("a woken turn belongs to the run of the turn that spawned its child", () => {
  const bytes = readFileSync(
    join(CASES, "woken-principal-mail-run", "log.jsonl"),
  );
  const events = knownEvents(verified(new Uint8Array(bytes)));
  const opener = runOpener(events);
  expect(opener?.type).toBe("message_received");
  expect(opener?.seq).toBe(6);
});
