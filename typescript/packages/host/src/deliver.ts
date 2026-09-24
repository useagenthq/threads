import { type ChannelAdapter, DeliveryOutcome } from "@threads/core";
import {
  type ArtifactStore,
  dispatched,
  type EventDraft,
  type Fence,
  HOST_SEND,
  type JsonObject,
  redactSecrets,
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
  /**
   * The host stopping: no send begins after it, and one that never reached the fence is not
   * waited on. One that passed the fence is waited on, under its lease, until it settles.
   */
  readonly stopping: AbortSignal;
};

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
  stopping: AbortSignal,
): Promise<void> {
  if (stopping.aborted) return;
  const branch = writer.lease.branchId;
  const s: Send = {
    adapter,
    writer,
    artifacts,
    callId: call.callId,
    op: call.op,
    key: `${branch}:${call.callId}`,
    stopping,
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
          name: HOST_SEND,
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
  // Not begun: a stopping host leaves it for the next one to issue.
  if (s.stopping.aborted) return;
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
  if (sent === "stale" || sent === "stopped") return;
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
    await untilStopped(s.stopping, backoff(n));
    return attempt(s, n + 1);
  }
  append(s, result(s.callId, true, "not_executed", `not sent: ${sent.kind}`));
}

/** "stale": the lease is lost; "stopped": the host stopped waiting. Either way, nothing more. */
type Performed = DeliveryOutcome | "stale" | "stopped";

async function perform(s: Send): Promise<Performed> {
  const credentials: Record<string, string> = {};
  for (const [name, secret] of Object.entries(s.adapter.secrets))
    credentials[name] = secret.reveal();
  const gate = sendFence(s);
  const sending = dispatched(gate.fence, () =>
    s.adapter.perform(s.op, s.key, credentials),
  );
  const first = await untilStopped(s.stopping, sending);
  // A send that never reached the fence is abandoned: the fence refuses it from now on, and it
  // stays begun for the next host to reconcile. One that passed it may still land, so it keeps
  // this writer's lease until it settles: released, a lookup elsewhere could find nothing and
  // send again while this request is still on its way.
  if (first === "stopped" && !gate.passed()) return first;
  const done = first === "stopped" ? await sending : first;
  // A refused fence means this writer lost its lease: it appends and sends nothing more.
  if (done.refused) return "stale";
  if (!done.outcome.ok)
    // A throw before any request passed the fence proves nothing left; after one, it may have.
    return {
      status: "delivery_error",
      kind: "transient",
      sent: done.sent ? "outcome_unknown" : "definite_not_sent",
    };
  const outcome = DeliveryOutcome.safeParse(done.outcome.value);
  // A malformed answer after dispatch is uncertainty, never a plain failure.
  return outcome.success
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
      : await untilStopped(
          s.stopping,
          within(fenceOf(s.writer), () => s.adapter.lookup(s.key, s.op)),
        );
  // Unanswered when the host stopped: still in doubt, for the next host to look up.
  if (looked === "stopped") return;
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
  // Stored text, so redacted like any result (C5); the event's copy is redacted by the writer.
  const bytes = new TextEncoder().encode(redactSecrets(platformRef));
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

function fenceOf(writer: Writer): Fence {
  return {
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

/**
 * A send's fence: the lease's, closed once the host stops, and remembering whether a request
 * passed it. Checked and marked in one step, so no request passes unseen after stop is decided.
 */
function sendFence(s: Send): { readonly fence: Fence; passed: () => boolean } {
  const lease = fenceOf(s.writer);
  let passed = false;
  return {
    fence: {
      fence: async () => {
        const live = await lease.fence();
        if (!live.ok) return live;
        if (s.stopping.aborted)
          return {
            ok: false,
            error: { code: "stale_epoch", message: "the host is stopping" },
          };
        passed = true;
        return live;
      },
    },
    passed: () => passed,
  };
}

/** `work`, or "stopped" once `stopping` is aborted, whichever comes first; `work` isn't cancelled. */
async function untilStopped<T>(
  stopping: AbortSignal,
  work: Promise<T>,
): Promise<T | "stopped"> {
  if (stopping.aborted) return "stopped";
  const stopped = Promise.withResolvers<"stopped">();
  const onAbort = (): void => stopped.resolve("stopped");
  stopping.addEventListener("abort", onAbort, { once: true });
  try {
    return await Promise.race([work, stopped.promise]);
  } finally {
    stopping.removeEventListener("abort", onAbort);
  }
}

async function backoff(n: number): Promise<void> {
  const { promise, resolve } = Promise.withResolvers<void>();
  setTimeout(resolve, Math.min(5_000, 250 * 2 ** n));
  await promise;
}
