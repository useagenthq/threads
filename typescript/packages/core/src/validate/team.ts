import type { KnownEvent } from "../log";
import type { Violation } from "./violation";

// Teams and background wakes (semantic rules 31-45) are in the schema before either runtime
// reduces them. Until the Teams build implements those rules, a reader refuses a log with any of
// their events, or any team form of an existing event, as it refuses a critical event it doesn't
// know: it never reduces one as ordinary work.

const TEAM_EVENTS: ReadonlySet<string> = new Set([
  "team_opened",
  "member_started",
  "member_idle",
  "member_ended",
  "member_observed",
  "monitor_set",
  "wait_started",
  "wait_finished",
  "woken",
  "message_sent",
  "message_received",
  "mail_refused",
  "ask_closed",
  "operator_request",
  "operator_refused",
  "message_policy_decided",
]);
const TEAM_PARKS: ReadonlySet<string> = new Set(["ask", "wait", "member"]);

/** The team form this event takes, if any. */
function teamForm(e: KnownEvent): string | undefined {
  if (TEAM_EVENTS.has(e.type)) return e.type;
  if (e.type === "thread_started") {
    if (e.data.team !== undefined) return "thread_started{team}";
    if (e.data.parent?.relation === "team_member")
      return "thread_started{parent: team_member}";
  }
  if (e.type === "user_input" && e.data.source === "team_task")
    return "user_input{team_task}";
  if (
    (e.type === "parked" ||
      e.type === "resumed" ||
      e.type === "park_escalated") &&
    TEAM_PARKS.has(e.data.address.kind)
  )
    return `${e.type}{${e.data.address.kind}}`;
  if (
    e.type === "turn_completed" &&
    (e.data.code === "pin_unavailable" || e.data.code === "pin_mismatch")
  )
    return `turn_completed{${e.data.code}}`;
  return undefined;
}

/** A refusal for a team event or team form, until the Teams build. */
export function checkNotYetTeam(e: KnownEvent): Violation {
  const form = teamForm(e);
  return form === undefined
    ? undefined
    : {
        code: "unsupported_critical_event",
        message: `${form} is not reduced by this version: the Teams build implements semantic rules 31-45`,
      };
}
