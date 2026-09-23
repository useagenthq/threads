import type { ChannelAdapter } from "@threads/core";
import {
  type JsonValue,
  type KnownEvent,
  knownEvents,
  type RunResult,
  storeConnection,
} from "@threads/core/host";
import type { z } from "zod";
import type { HostContext } from "./context";
import { sendOp } from "./deliver";
import type { Conversation } from "./inbox";

// What a finished channel run sends back: a completed turn's final response, or the approval
// cards of a run parked on approvals. Each rendered op is a host-issued channel_send with call_id
// send_<source seq>_<op index>, so recovery can never mint a different one.

type Json = z.infer<typeof JsonValue>;

export async function afterRun(
  ctx: HostContext,
  adapter: ChannelAdapter,
  conversation: Conversation,
  result: RunResult<Json>,
  lastInbound: number,
): Promise<void> {
  const { tenant } = conversation;
  if (result.status === "handed_off") {
    // The next message of the conversation routes to the target thread.
    const { db } = await storeConnection(ctx.store);
    db.run(
      "UPDATE channel_threads SET thread_id = ? WHERE tenant_id = ? AND thread_id = ?",
      [result.to_thread.id, tenant, result.thread.id],
    );
    return;
  }
  const { log, artifacts } = await ctx.open(tenant);
  const writer = log.acquire(
    result.thread.branch,
    `send-${crypto.randomUUID()}`,
  );
  if (!writer.ok) return;
  try {
    const events = knownEvents(writer.value.chain);
    for (const source of sources(events, result)) {
      const request = events.findLast((e) => e.type === "model_request");
      if (request === undefined) return;
      const ops = adapter.render(source);
      for (const [i, op] of ops.entries())
        await sendOp(adapter, writer.value, artifacts, {
          callId: `send_${source.seq}_${i}`,
          op: {
            ...op,
            address: conversation.address,
            installation_id: conversation.installation,
            last_inbound_at: lastInbound,
          },
          requestId: request.event_id,
        });
    }
  } finally {
    writer.value.release();
  }
}

/** The events whose rendering goes out after this result. */
function sources(
  events: readonly KnownEvent[],
  result: RunResult<Json>,
): readonly KnownEvent[] {
  if (result.status === "completed") {
    const done = events.findLastIndex((e) => e.type === "turn_completed");
    const response = events
      .slice(0, done)
      .findLast(
        (e) =>
          e.type === "model_response" || e.type === "model_response_recovered",
      );
    return response === undefined ? [] : [response];
  }
  if (result.status !== "parked") return [];
  const waiting = new Set(
    result.pending.filter((a) => a.kind === "approval").map((a) => a.id),
  );
  return events.filter(
    (e) => e.type === "approval_requested" && waiting.has(e.data.challenge_id),
  );
}
