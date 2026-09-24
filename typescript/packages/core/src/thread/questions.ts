import type { Fold } from "../fold/state";
import type { KnownEvent, Principal } from "../log";
import type { Ask } from "../tools/ask-user";

// Open ask_user questions as the log shows them (spec/schema/README.md, "Questions and
// remembered rules"): what a host posts to a channel and routes the asker's reply to.

export type OpenQuestion = {
  readonly callId: string;
  readonly ask: Ask;
  /** The principal whose user_input opened the asking turn: the only one who may answer. */
  readonly asker: Principal | undefined;
};

/** The principal whose user_input opened the turn with the call. */
export function askerOf(
  events: readonly KnownEvent[],
  callId: string,
): Principal | undefined {
  const at = events.findIndex(
    (e) => e.type === "tool_call" && e.data.call_id === callId,
  );
  return events
    .slice(0, Math.max(at, 0))
    .findLast((e) => e.type === "user_input")?.actor.principal;
}

/** The branch's open questions, oldest first. */
export function openQuestions(
  events: readonly KnownEvent[],
  fold: Fold,
): readonly OpenQuestion[] {
  return fold.parked.flatMap((address) => {
    const ask =
      address.kind === "input" ? fold.asks.get(address.id) : undefined;
    if (ask === undefined || ask === "invalid") return [];
    return [{ callId: address.id, ask, asker: askerOf(events, address.id) }];
  });
}
