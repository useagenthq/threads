import type { KnownEvent } from "../log";
import { type ParkAddress, sameAddress } from "./state";
import { mailRenders, monitorId } from "./team";

// Where the open (or last) turn of a log began: its user_input, its woken, or the received mail
// that opened it (spec/schema/README.md, "Which events open a turn"), read from the events alone
// with the fold's own rules. Mirrors Python's threads.reduce.openers.

function opens(
  e: KnownEvent,
  parks: readonly ParkAddress[],
  settle: ReadonlySet<string>,
): boolean {
  if (e.type === "user_input" || e.type === "woken") return true;
  if (e.type !== "message_received") return false;
  const env = e.data.envelope;
  const resolved = parks.every(
    (p) => p.kind === "member" && p.id === env.monitor_id,
  );
  return mailRenders(env, settle) && resolved;
}

/** The index of the event that opened the open (or last) turn; undefined before any turn. */
export function turnStart(events: readonly KnownEvent[]): number | undefined {
  if (events[0]?.type === "team_opened") return undefined;
  let inTurn = false;
  let start: number | undefined;
  let parks: readonly ParkAddress[] = [];
  const settle = new Set<string>();
  events.forEach((e, i) => {
    if (!inTurn && opens(e, parks, settle)) {
      inTurn = true;
      start = i;
    }
    if (e.type === "turn_completed") inTurn = false;
    else if (e.type === "parked") parks = [...parks, e.data.address];
    else if (e.type === "resumed")
      parks = parks.filter((p) => !sameAddress(p, e.data.address));
    else if (e.type === "wait_started")
      for (const m of e.data.members) settle.add(monitorId(e, m.name));
  });
  return start;
}

/**
 * The event that opened the run the last turn of `events` belongs to: the turn's own opener,
 * except that a `woken` belongs to the turn that spawned its first cause's child (followed back
 * through any earlier wakes). Undefined before any turn.
 */
export function runOpener(
  events: readonly KnownEvent[],
): KnownEvent | undefined {
  let upto = events;
  for (;;) {
    const start = turnStart(upto);
    const opener = start === undefined ? undefined : upto[start];
    if (opener?.type !== "woken" || start === undefined) return opener;
    const cause = opener.data.causes[0];
    const late = events.find((e) => e.event_id === cause);
    const call = late?.type === "tool_result_late" ? late.data.call_id : "";
    const spawned = upto.findIndex(
      (e) => e.type === "agent_spawned" && e.data.call_id === call,
    );
    if (spawned < 0 || spawned >= start) return undefined;
    upto = upto.slice(0, spawned + 1);
  }
}
