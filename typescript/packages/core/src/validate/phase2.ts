import type { KnownEvent, MailEnvelope } from "../log";
import type { Violation } from "./violation";

// Teams Phase 2 (host teams, host members, callers, turn failures and supervision; lane 29) is in
// the schema before either runtime reduces it. Until its build implements semantic rules 50-55,
// a reader refuses a log with any Phase 2 event or form, as it refuses a critical event it
// doesn't know: it never reduces one as ordinary work.

function envelopeForm(env: MailEnvelope): string | undefined {
  if ("caller" in env.from) return "a caller's mail";
  if (env.to !== "team_log" && "caller" in env.to) return "mail to a caller";
  return env.code === "turn_failed" ? "a turn_failed bounce" : undefined;
}

/** The Phase 2 form this event takes, if any. */
function phase2Form(e: KnownEvent): string | undefined {
  switch (e.type) {
    case "supervisor_decided":
      return e.type;
    case "team_opened":
      return e.data.kind === "host" ? "team_opened{kind: host}" : undefined;
    case "thread_started":
      return e.data.host_member === undefined
        ? undefined
        : "thread_started{host_member}";
    case "member_started":
      return e.data.host_member === undefined
        ? undefined
        : "member_started{host_member}";
    case "member_idle":
      return e.data.turn_failed === undefined
        ? undefined
        : "member_idle{turn_failed}";
    case "ask_closed":
      return e.data.outcome.status === "failed"
        ? "ask_closed{failed}"
        : undefined;
    case "budget_exceeded":
      return e.data.scope === "hop" ? "budget_exceeded{scope: hop}" : undefined;
    case "message_sent":
    case "message_received":
      return envelopeForm(e.data.envelope);
    default:
      return undefined;
  }
}

/** A refusal for a Phase 2 event or form, until the Teams Phase 2 build. */
export function checkNotYetPhase2(e: KnownEvent): Violation {
  const form = phase2Form(e);
  return form === undefined
    ? undefined
    : {
        code: "unsupported_critical_event",
        message: `${form} is not reduced by this version: the Teams Phase 2 build (lane 29) implements semantic rules 50-55`,
      };
}
