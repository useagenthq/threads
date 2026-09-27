import { TURN_KEPT } from "../fold/host";
import type { EventOf } from "../fold/state";
import type { MailEnvelope } from "../log";
import { type Facts, TEAM_LOG, type TeamLogEvents } from "./cross";
import { sameJson } from "./json";
import { replyAddress } from "./mail";

// Rule 43's clauses about what an answer carries (spec/schema/README.md, "Semantic rules" 43):
// a bounce's causal and payload, and that a reply to a caller answers that caller's own ask.
// Checked by the `team` runner and every index rebuild, never by one log's validate_next.

/**
 * Rule 43 (Phase 2): a turn_failed bounce answers its ask. Its causal is the failed turn's own
 * turn_completed in this log, and it carries the ask's provenance back to the asker's address.
 * Reference: spec/tools/fixtures/ref_host_cross.py::turn_failed_bounce.
 */
function turnFailed(
  env: MailEnvelope,
  log: TeamLogEvents,
  known: Facts,
): string | undefined {
  const end = log.events.find((e) => e.event_id === env.causal.event_id);
  if (end?.type !== "turn_completed" || TURN_KEPT.has(end.data.reason))
    return "a turn_failed bounce's causal is not its failed turn's turn_completed";
  const ask = env.ask_id === undefined ? undefined : known.sent.get(env.ask_id);
  if (ask === undefined) return undefined;
  const back = replyAddress(ask);
  return sameJson(env.provenance, ask.provenance) && sameJson(env.to, back)
    ? undefined
    : "a turn_failed bounce goes back to its asker, with its provenance";
}

/** Rule 43 (Phase 2): a reply to a caller answers an ask that caller sent. */
export function replyToCaller(
  env: MailEnvelope,
  known: Facts,
): string | undefined {
  const to = env.to;
  if (to === TEAM_LOG || !("caller" in to)) return undefined;
  const ask = env.ask_id === undefined ? undefined : known.sent.get(env.ask_id);
  return ask === undefined || sameJson(ask.from, to)
    ? undefined
    : "a reply to a caller answers that caller's ask";
}

/**
 * A bounce's causal: its failed turn's turn_completed for a turn_failed bounce (Phase 2), its
 * sender's own member_ended for an ask that end never answered (coordinator decision 6), else the
 * refuser's mail_refused. It names an ask exactly when it refused one.
 */
export function bounce(
  env: MailEnvelope,
  log: TeamLogEvents,
  known: Facts,
): string | undefined {
  if (env.code === "turn_failed") return turnFailed(env, log, known);
  const cause = log.events.find((e) => e.event_id === env.causal.event_id);
  if (cause?.type === "member_ended")
    return endedBounce(env, log, known, cause);
  if (cause?.type !== "mail_refused")
    return "a bounce's causal is not its mail_refused";
  const refused = known.sent.get(cause.data.mail_id);
  if (refused === undefined) return undefined;
  if (!sameJson(env.provenance, refused.provenance))
    return "a bounce's provenance is not its refused mail's";
  if (refused.kind !== "ask")
    return env.ask_id === undefined
      ? undefined
      : "only an ask's bounce names an ask";
  return env.ask_id === refused.ask_id && env.result !== undefined
    ? undefined
    : "an ask's bounce must name the ask and carry the result";
}

/**
 * Rule 43, the end's own bounce (coordinator decision 6): a member that ends bounces every ask it
 * had taken and never answered, so its causal is that member_ended rather than a mail_refused
 * there never was. It carries the end's result and the ask's provenance back to the asker.
 * Reference: spec/tools/fixtures/ref_host_cross.py::ended_bounce.
 */
function endedBounce(
  env: MailEnvelope,
  log: TeamLogEvents,
  known: Facts,
  end: EventOf<"member_ended">,
): string | undefined {
  if (env.ask_id === undefined)
    return "a bounce whose causal is the member's end names the ask it answers";
  if (!sameJson(env.result ?? null, end.data.result))
    return "an end's bounce carries the end's result";
  const ask = known.sent.get(env.ask_id);
  if (ask === undefined) return undefined;
  const took = log.events.some(
    (e) => e.type === "message_received" && e.data.mail_id === ask.mail_id,
  );
  if (!took) return "an end's bounce answers an ask the member had taken";
  return sameJson(env.provenance, ask.provenance) &&
    sameJson(env.to, replyAddress(ask))
    ? undefined
    : "an end's bounce goes back to its asker, with its provenance";
}
