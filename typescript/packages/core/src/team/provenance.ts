import type { EventOf } from "../fold/state";
import type { KnownEvent, Provenance } from "../log";
import type { Tx } from "../store/driver";
import type { Chain } from "../verify";
import { turnOpeners } from "./index";
import { mailEnvelope } from "./rows";

// Provenance (spec/schema/README.md, "Teams"): every mail and member_started a turn sends carries
// the (principal, root_request) of that turn, read from its opener. One turn has one authority
// and one budget root.

/** The provenance of the open turn, or of the last one; undefined before any turn. */
export async function turnProvenance(
  tx: Tx,
  chain: Chain,
): Promise<Provenance | undefined> {
  const opened = turnOpeners(chain.events);
  const events = chain.events.flatMap((l) =>
    l.kind === "event" ? [l.event] : [],
  );
  const opener = events.findLast((e) => opened.has(e.event_id));
  return opener === undefined
    ? undefined
    : await openerProvenance(tx, events, opened, opener);
}

async function openerProvenance(
  tx: Tx,
  events: readonly KnownEvent[],
  opened: ReadonlySet<string>,
  e: KnownEvent,
): Promise<Provenance | undefined> {
  switch (e.type) {
    case "message_received":
      return e.data.envelope.provenance;
    case "user_input":
      return await inputProvenance(tx, e);
    case "woken":
      return await wokenProvenance(tx, events, opened, e);
    default:
      return undefined;
  }
}

/** A task's user_input belongs to its task mail's request; any other input is a request. */
async function inputProvenance(
  tx: Tx,
  e: EventOf<"user_input">,
): Promise<Provenance | undefined> {
  if (e.data.mail_id !== undefined)
    return (await mailEnvelope(tx, e.data.mail_id))?.provenance;
  const principal = e.actor.principal;
  return {
    principal,
    root_request: { thread_id: e.thread_id, event_id: e.event_id },
    via: [],
  };
}

/** A woken turn belongs to the run that spawned its first cause's child. */
async function wokenProvenance(
  tx: Tx,
  events: readonly KnownEvent[],
  opened: ReadonlySet<string>,
  e: EventOf<"woken">,
): Promise<Provenance | undefined> {
  const late = events.find((x) => x.event_id === e.data.causes[0]);
  if (late?.type !== "tool_result_late") return undefined;
  const spawn = events.findIndex(
    (x) => x.type === "agent_spawned" && x.data.call_id === late.data.call_id,
  );
  const opener = events.slice(0, spawn).findLast((x) => opened.has(x.event_id));
  return opener === undefined
    ? undefined
    : await openerProvenance(tx, events, opened, opener);
}
