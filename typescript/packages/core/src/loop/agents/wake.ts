import { wakeBar } from "../../fold/wake";
import type { EventDraft } from "../../store";
import type { Session } from "../session";
import type { Spawned } from "./spawn";

// A late result recorded while the wake condition holds (fold/wake.ts wakeBar, rule 32) wakes
// the thread: the same append carries woken{causes}, which opens a turn of the run that spawned
// the children and acts for that run's principal (spec/schema/README.md, "Background wakes";
// rules 32 and 45).

/** The woken for these late results, or undefined when the thread must not wake. */
export function wakeDraft(
  s: Session,
  spawned: Spawned,
  calls: readonly string[],
  causes: readonly string[],
): EventDraft | undefined {
  if (causes.length === 0 || wakeBar(s.fold, calls) !== undefined)
    return undefined;
  // The spawning turn's opener acts for the run's principal: its user_input, turn-opening mail
  // or woken; mail joins a turn only when it shares the turn's run (rule 34).
  const at = s.events.indexOf(spawned);
  const opener = s.events
    .slice(0, at)
    .findLast(
      (e) =>
        e.type === "user_input" ||
        e.type === "woken" ||
        e.type === "message_received",
    );
  if (opener?.actor.principal === undefined) return undefined;
  return {
    type: "woken",
    type_version: 1,
    critical: true,
    actor: { kind: "host", principal: opener.actor.principal },
    data: { causes: [...causes] },
  };
}
