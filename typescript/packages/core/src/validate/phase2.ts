import type { KnownEvent } from "../log";
import type { Violation } from "./violation";

// Supervision (lane 29E) is in the schema before either runtime reduces it. Until its build
// implements semantic rule 51, a reader refuses a log with a supervisor decision or a supervised
// restart, as it refuses a critical event it doesn't know: it never reduces one as ordinary work.
// The rest of Teams Phase 2 — host teams, host members, callers, turn failures and the failed ask
// (rules 50 and 52-55) — is live (lane 29D, validate/host.ts).

/** The supervision form this event takes, if any. */
function supervisionForm(e: KnownEvent): string | undefined {
  if (e.type === "supervisor_decided") return e.type;
  return e.type === "member_started" && e.data.restart_of !== undefined
    ? "member_started{restart_of}"
    : undefined;
}

/** A refusal for a supervision event or form, until the Teams Phase 2 supervision build. */
export function checkNotYetPhase2(e: KnownEvent): Violation {
  const form = supervisionForm(e);
  return form === undefined
    ? undefined
    : {
        code: "unsupported_critical_event",
        message: `${form} is not reduced by this version: the Teams Phase 2 supervision build (lane 29E) implements semantic rule 51`,
      };
}
