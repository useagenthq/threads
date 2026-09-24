import { type EventOf, inputText, type KnownEvent } from "@threads/core/host";
import { agUiEvents, MODEL_STEP, started } from "./ag-ui";
import { foldAgUi, type UserTurn } from "./ag-ui-fold";
import { RunFacts } from "./facts";
import type { Chunk } from "./frame";

// An AG-UI replay's opening (spec/schema/ui/README.md, "Snapshot"): MESSAGES_SNAPSHOT of the
// thread's history through the snapshot point, then the open-state preamble. The stock client
// merges a snapshot by id, so it holds the whole history, not only the replayed run.

/**
 * The client's own id for a user message: the one it sent (client_message_id), else a UI
 * receipt's (runs recorded before the field), else the input's event id.
 */
export function userMessageId(
  e: EventOf<"user_input">,
  receipts: ReadonlyMap<string, string>,
): string {
  return e.data.client_message_id ?? receipts.get(e.event_id) ?? e.event_id;
}

/** MESSAGES_SNAPSHOT of `history`: the resolved chain from its start through the snapshot point. */
export function messagesSnapshot(
  history: readonly KnownEvent[],
  receipts: ReadonlyMap<string, string>,
): Chunk {
  const turns: {
    id: string;
    text: string;
    frames: Chunk[];
    facts: RunFacts;
  }[] = [];
  for (const e of history) {
    if (e.type === "user_input") {
      turns.push({
        id: userMessageId(e, receipts),
        text: inputText(e),
        frames: [],
        facts: new RunFacts(),
      });
      continue;
    }
    const turn = turns.at(-1);
    if (turn === undefined) continue;
    turn.frames.push(...agUiEvents(e, turn.facts));
    turn.facts.add(e);
  }
  const folded: readonly UserTurn[] = turns;
  return { type: "MESSAGES_SNAPSHOT", messages: foldAgUi(folded) };
}

/** What the log shows open at the snapshot point: a model step, running legacy subagents. */
export function preamble(facts: RunFacts): readonly Chunk[] {
  return [
    ...(facts.openStep() === undefined ? [] : [MODEL_STEP]),
    ...facts.running().map(started),
  ];
}
