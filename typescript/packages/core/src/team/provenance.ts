import type { EventOf } from "../fold/state";
import type { KnownEvent, Provenance } from "../log";
import type { SqliteDriver } from "../store/driver";
import type { Chain } from "../verify";
import { turnOpeners } from "./index";
import { mailEnvelope } from "./rows";

// Provenance (spec/schema/README.md, "Teams"): every mail and member_started a turn sends carries
// the (principal, root_request) of that turn, read from its opener. One turn has one authority
// and one budget root.

/** The provenance of the open turn, or of the last one; undefined before any turn. */
export function turnProvenance(
  db: SqliteDriver,
  chain: Chain,
): Provenance | undefined {
  const opened = turnOpeners(chain.events);
  const events = chain.events.flatMap((l) =>
    l.kind === "event" ? [l.event] : [],
  );
  const opener = events.findLast((e) => opened.has(e.event_id));
  return opener === undefined
    ? undefined
    : openerProvenance(db, events, opened, opener);
}

function openerProvenance(
  db: SqliteDriver,
  events: readonly KnownEvent[],
  opened: ReadonlySet<string>,
  e: KnownEvent,
): Provenance | undefined {
  switch (e.type) {
    case "message_received":
      return e.data.envelope.provenance;
    case "user_input":
      return inputProvenance(db, e);
    case "woken":
      return wokenProvenance(db, events, opened, e);
    default:
      return undefined;
  }
}

/** A task's user_input belongs to its task mail's request; any other input is a request. */
function inputProvenance(
  db: SqliteDriver,
  e: EventOf<"user_input">,
): Provenance | undefined {
  if (e.data.mail_id !== undefined)
    return mailEnvelope(db, e.data.mail_id)?.provenance;
  const principal = e.actor.principal;
  return {
    principal,
    root_request: { thread_id: e.thread_id, event_id: e.event_id },
    via: [],
  };
}

/** A woken turn belongs to the run that spawned its first cause's child. */
function wokenProvenance(
  db: SqliteDriver,
  events: readonly KnownEvent[],
  opened: ReadonlySet<string>,
  e: EventOf<"woken">,
): Provenance | undefined {
  const late = events.find((x) => x.event_id === e.data.causes[0]);
  if (late?.type !== "tool_result_late") return undefined;
  const spawn = events.findIndex(
    (x) => x.type === "agent_spawned" && x.data.call_id === late.data.call_id,
  );
  const opener = events.slice(0, spawn).findLast((x) => opened.has(x.event_id));
  return opener === undefined
    ? undefined
    : openerProvenance(db, events, opened, opener);
}
