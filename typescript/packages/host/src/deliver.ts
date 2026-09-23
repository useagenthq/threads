import { type ChannelAdapter, DeliveryOutcome } from "@threads/core";
import {
  type ArtifactStore,
  BranchId,
  type EventDraft,
  type JsonObject,
  type Writer,
  within,
} from "@threads/core/host";
import type { z } from "zod";

// Outbound replies are effects: every op is a channel_send tool_call with a
// deterministic call_id, its effect_begin durable before perform, and perform fenced at the real
// transport (the adapter's SDK sends through sandboxFetch inside within()). Delivery certainty
// decides what follows; an unknown outcome is never re-sent on a transient error or a backoff.

type Op = z.infer<typeof JsonObject>;
type Send = {
  readonly adapter: ChannelAdapter;
  readonly writer: Writer;
  readonly artifacts: ArtifactStore;
  readonly callId: string;
  readonly op: Op;
  readonly key: string;
};

export const SEND_TOOL = "channel_send";
const MAX_ATTEMPTS = 3;
const HOST = { kind: "host" } as const;
const base = { type_version: 1, critical: true } as const;

/** Sends `op` as call `callId` on the writer's branch, settling it as far as certainty allows. */
export async function sendOp(
  adapter: ChannelAdapter,
  writer: Writer,
  artifacts: ArtifactStore,
  call: {
    readonly callId: string;
    readonly op: Op;
    readonly requestId: string;
  },
): Promise<void> {
  const branch = writer.lease.branchId;
  const s: Send = {
    adapter,
    writer,
    artifacts,
    callId: call.callId,
    op: call.op,
    key: `${branch}:${call.callId}`,
  };
  const known = writer.chain.fold.calls.get(call.callId);
  if (known?.result !== undefined) return;
  if (known === undefined) {
    const opened = writer.append([
      {
        ...base,
        type: "tool_call",
        actor: HOST,
        data: {
          call_id: call.callId,
          name: SEND_TOOL,
          input: call.op,
          request_event_id: call.requestId,
        },
      },
      {
        ...base,
        type: "permission_decision",
        actor: HOST,
        data: {
          call_id: call.callId,
          decision: "allow",
          source: "policy",
          rule_id: "channel_delivery",
        },
      },
    ]);
    if (!opened.ok) return;
  }
  const status = writer.chain.fold.effects.get(s.key)?.status;
  // A send already begun and unsettled (a crash) is reconciled, never blindly re-sent.
  if (status === "begun" || status === "unknown")
    return reconcile(s, attempts(s));
  return attempt(s, attempts(s) + 1);
}

function attempts(s: Send): number {
  return s.writer.chain.events.filter(
    (e) =>
      e.kind === "event" &&
      e.event.type === "effect_begin" &&
      e.event.data.call_id === s.callId,
  ).length;
}

async function attempt(s: Send, n: number): Promise<void> {
  const begun = s.writer.append([
    {
      ...base,
      type: "effect_begin",
      actor: HOST,
      data: { call_id: s.callId, attempt: n },
    },
  ]);
  if (!begun.ok) return;
  const sent = await perform(s);
  if (sent === "stale") return;
  if (sent.status === "sent") return commit(s, sent.platform_ref, "adapter");
  if (sent.sent === "outcome_unknown") {
    const unknown = append(s, {
      ...base,
      type: "effect_unknown",
      actor: HOST,
      data: { call_id: s.callId, reason: "transport_error" },
    });
    return unknown ? reconcile(s, n) : undefined;
  }
  const settled = append(s, {
    ...base,
    type: "effect_resolved",
    actor: HOST,
    data: { call_id: s.callId, outcome: "not_sent", by: "adapter" },
  });
  if (!settled) return;
  // definite_not_sent: rate_limited and transient may re-send under the same key.
  if (sent.kind !== "permanent" && n < MAX_ATTEMPTS) {
    await backoff(n);
    return attempt(s, n + 1);
  }
  append(s, result(s.callId, true, "not_executed", `not sent: ${sent.kind}`));
}

type Performed = DeliveryOutcome | "stale";

async function perform(s: Send): Promise<Performed> {
  const credentials: Record<string, string> = {};
  for (const [name, secret] of Object.entries(s.adapter.secrets))
    credentials[name] = secret.reveal();
  const done = await within(context(s.writer), () =>
    s.adapter.perform(s.op, s.key, credentials),
  );
  if (!done.ok && done.error.stale !== undefined) return "stale";
  const outcome = done.ok ? DeliveryOutcome.safeParse(done.value) : undefined;
  // A throw or a malformed answer after dispatch is uncertainty, never a plain failure.
  return outcome?.success === true
    ? outcome.data
    : { status: "delivery_error", kind: "transient", sent: "outcome_unknown" };
}

/**
 * as applies it: provider dedup inside the window, then a lookup; only
 * found or a final not_found settles; anything else parks for a human.
 */
async function reconcile(s: Send, n: number): Promise<void> {
  const { capabilities } = s.adapter;
  if (withinDedup(s, capabilities.dedup_window_ms)) {
    const retry = append(
      s,
      resolved(s.callId, "safe_to_retry", "provider_dedup"),
    );
    return retry ? attempt(s, n + 1) : undefined;
  }
  const looked =
    capabilities.lookup === "none"
      ? undefined
      : await within(context(s.writer), () => s.adapter.lookup(s.key, s.op));
  if (looked !== undefined && !looked.ok && looked.error.stale !== undefined)
    return;
  const answer = looked?.ok === true ? looked.value : undefined;
  if (answer?.status === "found") return commit(s, answer.value, "reconcile");
  if (answer?.status === "not_found" && capabilities.lookup === "final") {
    const retry = append(s, resolved(s.callId, "safe_to_retry", "reconcile"));
    return retry ? attempt(s, n + 1) : undefined;
  }
  append(s, {
    ...base,
    type: "parked",
    actor: HOST,
    data: { address: { kind: "effect", id: s.key }, reason: "effect_unknown" },
  });
}

function commit(
  s: Send,
  platformRef: string,
  by: "adapter" | "reconcile",
): void {
  const bytes = new TextEncoder().encode(platformRef);
  const ref = {
    sha256: s.artifacts.put(bytes),
    bytes: bytes.length,
    media_type: "text/plain",
  };
  const settled: EventDraft =
    by === "adapter"
      ? {
          ...base,
          type: "effect_commit",
          actor: HOST,
          data: {
            call_id: s.callId,
            result_ref: ref,
            provider_receipt: platformRef,
          },
        }
      : {
          ...base,
          type: "effect_resolved",
          actor: HOST,
          data: {
            call_id: s.callId,
            outcome: "confirmed_success",
            by: "reconcile",
            result_ref: ref,
          },
        };
  s.writer.append([
    settled,
    result(s.callId, false, "executed", `sent ${platformRef}`),
  ]);
}

function resolved(
  callId: string,
  outcome: "safe_to_retry",
  by: "provider_dedup" | "reconcile",
): EventDraft {
  return {
    ...base,
    type: "effect_resolved",
    actor: HOST,
    data: { call_id: callId, outcome, by },
  };
}

function result(
  callId: string,
  isError: boolean,
  origin: "executed" | "not_executed",
  preview: string,
): EventDraft {
  return {
    ...base,
    type: "tool_result",
    actor: HOST,
    data: {
      call_id: callId,
      is_error: isError,
      origin,
      preview,
      completeness: "complete",
    },
  };
}

/** Inside the provider's dedup window from the first attempt, a re-send is deduplicated. */
function withinDedup(s: Send, windowMs: number | undefined): boolean {
  if (windowMs === undefined) return false;
  const first = s.writer.chain.events.find(
    (e) =>
      e.kind === "event" &&
      e.event.type === "effect_begin" &&
      e.event.data.call_id === s.callId,
  );
  return first?.kind === "event" && Date.now() - first.event.time < windowMs;
}

function append(s: Send, draft: EventDraft): boolean {
  return s.writer.append([draft]).ok;
}

function context(writer: Writer): Parameters<typeof within>[0] {
  return {
    authority: {
      kind: "owner",
      branch_id: BranchId.parse(writer.lease.branchId),
      epoch: writer.lease.epoch,
    },
    fence: async () => {
      const live = writer.fence();
      return live.ok
        ? { ok: true, value: undefined }
        : {
            ok: false,
            error: { code: "stale_epoch", message: live.error.message },
          };
    },
  };
}

async function backoff(n: number): Promise<void> {
  const { promise, resolve } = Promise.withResolvers<void>();
  setTimeout(resolve, Math.min(5_000, 250 * 2 ** n));
  await promise;
}
