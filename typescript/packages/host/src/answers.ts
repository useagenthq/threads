import {
  type ChainEvent,
  type EventDraft,
  type EventId,
  knownEvents,
  matchAnswer,
  openQuestions,
  type Principal,
  principalKey,
  resumed,
  type SqliteDriver,
  type Writer,
} from "@threads/core/host";
import { consumes } from "./inbox";

// A channel reply to an open ask_user question (spec/schema/README.md, "Questions and remembered
// rules"): the asker's next message answers the oldest open question. It is consumed with what
// it records, in one transaction: the answer and resumed, or, for a reply that matches no
// option, answer_rejected, whose correction the outbound path then posts.

/** What a message did to the thread's open questions. */
export type Replied = "answered" | "rejected" | "held" | "busy";

type Reply = {
  readonly inboxId: number;
  readonly principal: Principal;
  readonly text: string;
  /** The reply's channel_delivery. */
  readonly delivery: EventDraft;
};

/**
 * held: the thread waits on a question another principal must answer, so the message waits as
 * ordinary input; busy: no question is open, or the append failed.
 */
export function replyToQuestion(
  w: Writer,
  db: SqliteDriver,
  reply: Reply,
): Replied {
  const oldest = openQuestions(knownEvents(w.chain), w.chain.fold)[0];
  if (oldest === undefined) return "busy";
  if (
    oldest.asker === undefined ||
    principalKey(oldest.asker) !== principalKey(reply.principal)
  )
    return "held";
  const recorded = matchAnswer(oldest.ask, reply.text);
  const address = { kind: "input", id: oldest.callId } as const;
  const done = w.fenced(() => {
    const delivered = w.append([reply.delivery]);
    if (!delivered.ok) return delivered;
    const cause = idOf(delivered.value.at(-1));
    if (recorded === undefined)
      return w.append(
        [rejection(oldest.callId, cause)],
        consumes(db, reply.inboxId),
      );
    const answered = w.append([answer(oldest.callId, recorded, reply)]);
    if (!answered.ok) return answered;
    return w.append(
      [resumed(address, idOf(answered.value.at(-1)))],
      consumes(db, reply.inboxId),
    );
  });
  if (!done.ok) return "busy";
  return recorded === undefined ? "rejected" : "answered";
}

function idOf(line: ChainEvent | undefined): EventId {
  if (line?.kind !== "event")
    throw new Error("the append just recorded a known event");
  return line.event.event_id;
}

function rejection(callId: string, delivery: EventId): EventDraft {
  return {
    type: "answer_rejected",
    type_version: 1,
    critical: true,
    actor: { kind: "host" },
    data: {
      call_id: callId,
      delivery_event_id: delivery,
      reason: "not_an_option",
    },
  };
}

function answer(callId: string, text: string, reply: Reply): EventDraft {
  return {
    type: "tool_result",
    type_version: 1,
    critical: true,
    actor: { kind: "user", principal: reply.principal },
    data: {
      call_id: callId,
      is_error: false,
      origin: "answered",
      completeness: "complete",
      preview: text,
    },
  };
}
