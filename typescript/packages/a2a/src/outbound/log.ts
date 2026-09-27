import type { ArtifactRef, KnownEvent } from "threadsai/adapter";
import type { Wire } from "../protocol";

// What an outbound call reads back out of its own thread's log. Process memory is not allowed to
// decide any of it: a re-dispatch after a crash runs in a new process, and it must pin the same
// card, send the same bytes and reconcile against the same context as the attempt it follows.

/** The card a thread pinned for one remote, and the one interface it therefore calls. */
export type Pinned = {
  readonly cardRef: ArtifactRef;
  readonly wire: Wire;
};

export function pinnedCardOf(
  events: readonly KnownEvent[],
  remote: string,
): Pinned | undefined {
  const found = events.findLast(
    (e) => e.type === "remote_card" && e.data.remote === remote,
  );
  if (found?.type !== "remote_card") return undefined;
  const { card_ref, interface_url, binding } = found.data;
  return { cardRef: card_ref, wire: { url: interface_url, binding } };
}

/** The `remote_call` of one call id: the stored bytes and ids every attempt of it reuses. */
export type Called = {
  readonly remote: string;
  readonly messageId: string;
  readonly contextId: string;
  readonly taskId: string | undefined;
  readonly requestRef: ArtifactRef;
};

export function calledIn(
  events: readonly KnownEvent[],
  callId: string,
): Called | undefined {
  const found = events.find(
    (e) => e.type === "remote_call" && e.data.call_id === callId,
  );
  if (found?.type !== "remote_call") return undefined;
  const d = found.data;
  return {
    remote: d.remote,
    messageId: d.message_id,
    contextId: d.context_id,
    taskId: d.task_id,
    requestRef: d.request_ref,
  };
}

/**
 * The tenant this thread belongs to, read from its own log rather than from the running process, so
 * the bytes a re-dispatch sends are a function of the log alone — and the same in both languages.
 */
export function tenantOf(events: readonly KnownEvent[]): string {
  const first = events.find((e) => e.type === "user_input");
  return first?.type === "user_input" && first.actor.kind === "user"
    ? first.actor.principal.tenant
    : "";
}

/**
 * Whether **this thread's own log** created `taskId` with `remote`. All tenants' calls share one
 * host credential, so without this check the partner would happily show any caller any task.
 */
export function ownsTask(
  events: readonly KnownEvent[],
  remote: string,
  taskId: string,
): boolean {
  const mine = new Set(
    events.flatMap((e) =>
      e.type === "remote_call" && e.data.remote === remote
        ? [e.data.call_id]
        : [],
    ),
  );
  return events.some(
    (e) =>
      (e.type === "remote_call" &&
        e.data.remote === remote &&
        e.data.task_id === taskId) ||
      (e.type === "remote_task_state" &&
        e.data.task_id === taskId &&
        mine.has(e.data.call_id)) ||
      (e.type === "effect_commit" &&
        e.data.provider_receipt === taskId &&
        mine.has(e.data.call_id)),
  );
}

/** The call whose `remote_call` created `taskId`: the call an observation of it belongs to. */
export function taskOwner(
  events: readonly KnownEvent[],
  remote: string,
  taskId: string,
): string | undefined {
  const opened = events.find(
    (e) =>
      e.type === "effect_commit" &&
      e.data.provider_receipt === taskId &&
      events.some(
        (c) =>
          c.type === "remote_call" &&
          c.data.remote === remote &&
          c.data.call_id === e.data.call_id,
      ),
  );
  return opened?.type === "effect_commit" ? opened.data.call_id : undefined;
}

/** The states already observed for a task, so the same state and bytes are not appended twice. */
export function observed(
  events: readonly KnownEvent[],
  callId: string,
): ReadonlySet<string> {
  return new Set(
    events.flatMap((e) =>
      e.type === "remote_task_state" && e.data.call_id === callId
        ? [`${e.data.state}:${e.data.status_ref?.sha256 ?? ""}`]
        : [],
    ),
  );
}

/**
 * The request id this thread arrived under, when it came in over A2A: one request's calls then
 * share a provenance id across hops. The claim is untrusted, so only its shape is read.
 */
export function inboundClaim(events: readonly KnownEvent[]): {
  readonly request: string | undefined;
  readonly hops: number;
} {
  const first = events.find(
    (e) => e.type === "user_input" && e.data.a2a !== undefined,
  );
  const claims =
    first?.type === "user_input" ? first.data.a2a?.claims : undefined;
  const request = claims?.["request"];
  const hops = claims?.["hops"];
  return {
    request: typeof request === "string" ? request : undefined,
    hops:
      typeof hops === "number" && Number.isInteger(hops) && hops >= 0
        ? hops
        : 0,
  };
}
