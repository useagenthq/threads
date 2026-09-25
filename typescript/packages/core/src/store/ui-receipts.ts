import { sha256Hex } from "../hash";
import { canonicalize, type KnownEvent, principalKey } from "../log";
import type { Tx } from "./driver";

// The `ui` run receipts an import rebuilds (spec/schema/ui/README.md, "Bodies"): a web UI's
// retry finds its run by `<thread_id>:<client message id>`, so every user_input that carries
// client_message_id gets its receipt back in the import's transaction, as the host wrote it.

/** The run_receipts operation of a run started through a UI route. */
export const UI_RECEIPT = "ui";

/**
 * A `ui` receipt's body hash: the run's input only (the pinned agent name, the text, the
 * source), so a retry that changes any other client field still matches.
 */
export function uiBodyHash(agent: string, text: string): string | undefined {
  const body = canonicalize({ agent, input: text, source: "api" });
  return body.ok ? sha256Hex(body.value) : undefined;
}

/** The text a user_input carries: its text, or its text parts joined. */
export function inputText(
  e: Extract<KnownEvent, { type: "user_input" }>,
): string {
  if (e.data.text !== undefined) return e.data.text;
  return (e.data.content ?? [])
    .map((p) => (p.type === "text" ? p.text : ""))
    .join("");
}

/** Writes the receipt of each UI-started run among `events`; an existing receipt stays. */
export async function uiReceiptRows(
  tx: Tx,
  tenant: string,
  events: readonly KnownEvent[],
): Promise<void> {
  const started = events.find((e) => e.type === "thread_started");
  if (started?.type !== "thread_started") return;
  for (const e of events) {
    if (e.type !== "user_input" || e.data.client_message_id === undefined)
      continue;
    const hash = uiBodyHash(started.data.agent_name, inputText(e));
    if (hash === undefined) continue;
    await tx.run(
      `INSERT INTO run_receipts (tenant_id, operation, idempotency_key, principal_key, body_hash,
        thread_id, branch_id, run_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT DO NOTHING`,
      [
        tenant,
        UI_RECEIPT,
        `${e.thread_id}:${e.data.client_message_id}`,
        principalKey(e.actor.principal),
        hash,
        e.thread_id,
        e.branch_id,
        e.event_id,
        e.time,
      ],
    );
  }
}
