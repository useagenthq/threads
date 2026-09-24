import type { KnownEvent, ParkAddress } from "@threads/core/host";

// Where a human input stands in the log: an approval challenge open, decided or expired; an
// ask_user question open, answered or closed. The UI routes read it to make a repeated answer
// a no-op and to name what the log recorded when an answer arrives too late.

export type Recorded =
  | "granted"
  | "denied"
  | "expired"
  | "answered"
  | "cancelled";

/** The logged decision on a challenge, if any. */
export function decisionOf(
  events: readonly KnownEvent[],
  challengeId: string,
): "granted" | "denied" | undefined {
  const decided = events.findLast(
    (e) =>
      (e.type === "approval_granted" || e.type === "approval_denied") &&
      e.data.challenge_id === challengeId,
  );
  if (decided === undefined) return undefined;
  return decided.type === "approval_granted" ? "granted" : "denied";
}

export type Interrupt =
  | {
      readonly kind: "approval";
      readonly state: "open" | Recorded;
      /** Whether the branch is still parked on it (an expired challenge stays parked). */
      readonly parked: boolean;
    }
  | {
      readonly kind: "question";
      readonly state: "open" | Recorded;
      readonly answer?: string;
    }
  | { readonly kind: "other" }
  | { readonly kind: "unknown" };

/** What interrupt id `id` names in the thread, and where it stands at `now`. */
export function interruptOf(
  events: readonly KnownEvent[],
  parked: readonly ParkAddress[],
  id: string,
  now: number,
): Interrupt {
  const requested = events.findLast(
    (e) => e.type === "approval_requested" && e.data.challenge_id === id,
  );
  if (requested?.type === "approval_requested") {
    const decided = decisionOf(events, id);
    const held = parked.some((a) => a.kind === "approval" && a.id === id);
    if (decided !== undefined)
      return { kind: "approval", state: decided, parked: held };
    if (requested.data.expires_at <= now)
      return { kind: "approval", state: "expired", parked: held };
    return { kind: "approval", state: "open", parked: held };
  }
  if (parked.some((a) => a.kind === "input" && a.id === id))
    return { kind: "question", state: "open" };
  const asked = events.some(
    (e) =>
      e.type === "parked" &&
      e.data.address.kind === "input" &&
      e.data.address.id === id,
  );
  if (asked) {
    const result = events.findLast(
      (e) => e.type === "tool_result" && e.data.call_id === id,
    );
    return result?.type === "tool_result" && result.data.origin === "answered"
      ? { kind: "question", state: "answered", answer: result.data.preview }
      : { kind: "question", state: "cancelled" };
  }
  return parked.some((a) => a.id === id)
    ? { kind: "other" }
    : { kind: "unknown" };
}
