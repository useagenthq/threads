import { assertNever } from "../../assert-never";
import type { EventOf } from "../../fold/state";
import { AskId, type KnownEvent, type MailEnvelope } from "../../log";
import { knownEvents } from "../../reduce";
import type { LogStore } from "../../store";
import type { ArtifactStore } from "../../store/artifacts";
import { reading } from "../../store/driver";
import type { AskOutcome, Waited } from "../../team/results";
import { teamRow } from "../../team/rows";
import type { AskStatus } from "./handle-types";
import { hydrated, readText } from "./hydrate";

// What the team log says of an operator's ask or wait (spec/api.json AskStatus, TeamAskResult,
// TeamWaitResult): pure reads, hydrated as every result API is (a {ref} read back and verified,
// StoreCorruptError when it can't be).

type Store = { readonly log: LogStore; readonly artifacts: ArtifactStore };

async function teamLog(
  store: Store,
  team: string,
): Promise<readonly KnownEvent[]> {
  const row = await reading(store.log.driver, (tx) => teamRow(tx, team));
  if (row === undefined) throw new Error(`no team ${team}`);
  const read = await store.log.read(row.team_log_branch_id);
  if (!read.ok) throw new Error(`team log of ${team}: ${read.error.message}`);
  return knownEvents(read.value);
}

/** An ask's state: open with its deadline, its outcome once closed, or not_found. */
export async function askStatus(
  store: Store,
  team: string,
  askId: string,
): Promise<AskStatus> {
  const events = await teamLog(store, team);
  const closed = events.find(
    (e): e is EventOf<"ask_closed"> =>
      e.type === "ask_closed" && e.data.ask_id === askId,
  );
  if (closed !== undefined) return outcomeOf(store, events, closed);
  const asked = sentAsk(events, askId);
  const id = AskId.parse(askId);
  return asked?.deadline === undefined
    ? { status: "not_found", askId: id }
    : { status: "open", askId: id, deadline: asked.deadline };
}

/** The ask's outcome once the team log closed it. */
export async function askOutcome(
  store: Store,
  team: string,
  askId: string,
): Promise<AskOutcome | undefined> {
  const got = await askStatus(store, team, askId);
  return got.status === "open" || got.status === "not_found" ? undefined : got;
}

function sentAsk(
  events: readonly KnownEvent[],
  askId: string,
): MailEnvelope | undefined {
  return events.find(
    (e): e is EventOf<"message_sent"> =>
      e.type === "message_sent" &&
      e.data.envelope.kind === "ask" &&
      e.data.envelope.ask_id === askId,
  )?.data.envelope;
}

async function outcomeOf(
  store: Store,
  events: readonly KnownEvent[],
  closed: EventOf<"ask_closed">,
): Promise<AskOutcome> {
  const askId = closed.data.ask_id;
  const outcome = closed.data.outcome;
  switch (outcome.status) {
    case "answered": {
      const reply = events.find(
        (e): e is EventOf<"message_received"> =>
          e.type === "message_received" && e.data.mail_id === outcome.reply,
      )?.data.envelope;
      if (reply === undefined || "operator" in reply.from)
        throw new Error(`ask ${askId} was answered by no received reply`);
      const text = await bodyText(store, reply);
      return { status: "answered", askId, text, member: reply.from };
    }
    case "member_ended":
      return {
        status: "member_ended",
        askId,
        result: await hydrated(outcome.result, store.artifacts),
      };
    case "timed_out":
    case "cancelled":
      return { status: outcome.status, askId };
    default:
      return assertNever(outcome);
  }
}

async function bodyText(store: Store, env: MailEnvelope): Promise<string> {
  const body = env.body;
  if (body?.text !== undefined) return body.text;
  if (body?.ref === undefined) throw new Error("a reply has a text body");
  return readText(store.artifacts, body.ref);
}

/** The wait's outcome once the team log finished it. */
export async function waitOutcome(
  store: Store,
  team: string,
  waitId: string,
): Promise<Waited | undefined> {
  const done = (await teamLog(store, team)).find(
    (e): e is EventOf<"wait_finished"> =>
      e.type === "wait_finished" && e.data.wait_id === waitId,
  );
  if (done === undefined) return undefined;
  const { finished, parked, pending, timed_out } = done.data;
  const results = [];
  for (const r of finished) results.push(await hydrated(r, store.artifacts));
  return {
    status: "waited",
    finished: results,
    parked,
    pending,
    timedOut: timed_out,
  };
}
