import { z } from "zod";
import { canonicalize, type KnownEvent } from "../log";
import { failureOf } from "../loop/failure";
import type { EventDraft } from "../store/admit";
import type { Settlement } from "./settle";

// How a team thread's turn end settles it (design §2.4, the outcome map): end_turn leaves the
// member idle with its answer; every other end ends it. A lead's handoff is handed_off; a member
// can't hand off (setup refuses it: handoff_in_team).

type Item = KnownEvent | EventDraft;

/** The settlement of the turn `batch` ends, read from the turn's events and the batch. */
export function settlementOf(
  turn: readonly Item[],
  batch: readonly Item[],
): Settlement | undefined {
  const all = [...turn, ...batch];
  const end = batch.findLast((e) => e.type === "turn_completed");
  if (end?.type !== "turn_completed") return undefined;
  const { reason, code } = end.data;
  switch (reason) {
    case "end_turn":
      return { status: "completed", output: answer(all) };
    case "cancelled":
      return { status: "cancelled" };
    case "budget_exhausted": {
      const why = all.findLast((e) => e.type === "budget_exceeded");
      if (why?.type !== "budget_exceeded")
        throw new Error("budget_exhausted records why");
      return { status: "budget_exhausted", budget: why.data };
    }
    case "handoff": {
      const handoff = all.findLast((e) => e.type === "handoff");
      if (handoff?.type !== "handoff")
        throw new Error("a handoff's turn records its handoff");
      return { status: "handed_off", to_thread: handoff.data.to_thread_id };
    }
    default: {
      const error = failureOf({
        reason,
        ...(code === undefined ? {} : { code }),
      });
      if (error === undefined) throw new Error(`${reason} is a failed end`);
      return { status: "failed", error };
    }
  }
}

/** The turn's answer: its accepted structured value as RFC 8785 JSON, else its last text. */
function answer(all: readonly Item[]): string {
  const accepted = all.findLast(
    (e) => e.type === "output_validated" && e.data.outcome === "accepted",
  );
  if (
    accepted?.type === "output_validated" &&
    accepted.data.value !== undefined
  ) {
    const json = canonicalize(z.json().parse(accepted.data.value));
    if (json.ok) return json.value;
  }
  const said = all.findLast(
    (e) => e.type === "model_response" || e.type === "model_response_recovered",
  );
  if (
    said?.type !== "model_response" &&
    said?.type !== "model_response_recovered"
  )
    return "";
  return said.data.content
    .map((part) => (part.type === "text" ? part.text : ""))
    .join("");
}
