import type { z } from "zod";
import type { MailEnvelope } from "../log";
import type { EventDraft } from "../store/admit";
import type { Tx } from "../store/driver";
import { TEAM_CONSTANTS } from "./constants";
import { memberRows } from "./rows";

// Mail envelopes (spec/schema/README.md, "Teams", Mail): what a sender's message_sent records and
// every receipt copies byte for byte.

export type Envelope = z.input<typeof MailEnvelope>;
type Body = NonNullable<Envelope["body"]>;
type Address = Envelope["to"];

/** Stores text in the content-addressed store before the append that names it. */
/** Stores text as an artifact (content-addressed, so a retried attempt stores it again harmlessly). */
export type PutText = (text: string) => Promise<NonNullable<Body["ref"]>>;

/** The mail's `to`: the member whose branch it is, at its generation, or the team log. */
export async function addressOf(
  tx: Tx,
  team: string,
  branch: string,
): Promise<Address> {
  const row = (await memberRows(tx, team)).find((r) => r.branch_id === branch);
  return row === undefined
    ? "team_log"
    : { name: row.name, generation: row.generation };
}

/** A text body: inline up to the inline cap, else an artifact ref written before the append. */
export async function bodyOf(
  text: string,
  put: PutText,
  cap: number = TEAM_CONSTANTS.inlineCapBytes,
): Promise<Body> {
  return new TextEncoder().encode(text).length > cap
    ? { ref: await put(text) }
    : { text };
}

/** message_sent{envelope}, from the sending writer. */
export function sent(envelope: Envelope): EventDraft {
  return {
    type: "message_sent",
    type_version: 1,
    critical: true,
    actor: { kind: "host" },
    data: { envelope },
  };
}

/** message_received{mail_id, envelope}: its actor is the mail's provenance principal. */
export function received(envelope: MailEnvelope): EventDraft {
  return {
    type: "message_received",
    type_version: 1,
    critical: true,
    actor: { kind: "host", principal: envelope.provenance.principal },
    data: { mail_id: envelope.mail_id, envelope },
  };
}

/** mail_refused{mail_id, code}: the recipient's writer returns pending mail. */
export function refused(mailId: string): EventDraft {
  return {
    type: "mail_refused",
    type_version: 1,
    critical: true,
    actor: { kind: "host" },
    data: { mail_id: mailId, code: "member_ended" },
  };
}
