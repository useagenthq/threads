import {
  type ChannelAdapter,
  type DeliveryOutcome,
  FenceRefused,
  type Fetch,
  sandboxFetch,
} from "@threads/core/adapter";
import { z } from "zod";

// Sends over the Graph API with fetch. Meta publishes no maintained official JS SDK for the
// WhatsApp Cloud API (the old `whatsapp` npm package is archived), so there is no SDK to wrap
// and no dependency. The request leaves through sandboxFetch, so the host's fence (perform runs
// inside within()) is re-checked as it is sent; a FenceRefused is never caught here.

export const SESSION_WINDOW_MS = 86_400_000;

type Event = Parameters<ChannelAdapter["render"]>[0];
type Op = ReturnType<ChannelAdapter["render"]>[number];

export function render(event: Event): readonly Op[] {
  if (event.type === "model_response") {
    const text = event.data.content
      .map((part) => (part.type === "text" ? part.text : ""))
      .join("");
    return renderText(text);
  }
  if (event.type === "approval_requested") {
    const { challenge_id, call_id, args_hash } = event.data;
    const text = `Approve tool call ${call_id}? (arguments sha256 ${args_hash.slice(0, 12)})`;
    return [{ kind: "approval", challenge_id, text }];
  }
  return [];
}

/** A plain message: an ask_user question or its correction, or a reply's text. */
export function renderText(text: string): readonly Op[] {
  // ponytail: no splitting past limits.text_bytes; a longer reply fails permanent at Meta.
  return text === "" ? [] : [{ kind: "text", text }];
}

// The host sets address (the recipient wa_id) and installation_id (the phone number id).
// last_inbound_at is when the recipient last wrote to us: WhatsApp takes free-form messages
// only inside the 24-hour session window after it.
const Routed = {
  address: z.string().min(1),
  installation_id: z.string().min(1),
  last_inbound_at: z.number().int().optional(),
};
const OutboundOp = z.discriminatedUnion("kind", [
  z.strictObject({
    kind: z.literal("text"),
    text: z.string().min(1),
    ...Routed,
  }),
  z.strictObject({
    kind: z.literal("approval"),
    challenge_id: z.uuid(),
    text: z.string().min(1),
    ...Routed,
  }),
]);
type OutboundOp = z.infer<typeof OutboundOp>;

function message(op: OutboundOp): Record<string, unknown> {
  if (op.kind === "text")
    return { type: "text", text: { body: op.text, preview_url: false } };
  const button = (verb: "approve" | "deny", title: string) => ({
    type: "reply",
    reply: { id: `${verb}:${op.challenge_id}`, title },
  });
  return {
    type: "interactive",
    interactive: {
      type: "button",
      body: { text: op.text },
      action: {
        buttons: [button("approve", "Approve"), button("deny", "Deny")],
      },
    },
  };
}

const failed = (
  kind: "rate_limited" | "transient" | "permanent",
  sent: "definite_not_sent" | "outcome_unknown",
): DeliveryOutcome => ({ status: "delivery_error", kind, sent });

const Sent = z.looseObject({
  messages: z.tuple([z.looseObject({ id: z.string().min(1) })], z.unknown()),
});
const GraphError = z.looseObject({
  error: z.looseObject({ code: z.number().int() }),
});
/** Graph rate and pair-rate limit codes: Meta refused the send. */
const THROTTLED: ReadonlySet<number> = new Set([130429, 131048, 131056]);
/** Failures to connect: no byte of the request was written. */
const NOT_CONNECTED: ReadonlySet<unknown> = new Set([
  "ConnectionRefused",
  "ECONNREFUSED",
  "ENOTFOUND",
  "EAI_AGAIN",
  "FailedToOpenSocket",
]);

function notConnected(error: unknown): boolean {
  for (let e = error; e instanceof Error; e = e.cause)
    if ("code" in e && NOT_CONNECTED.has(e.code)) return true;
  return false;
}

async function outcomeOf(response: Response): Promise<DeliveryOutcome> {
  let body: unknown;
  try {
    body = await response.json();
  } catch {
    body = undefined;
  }
  if (response.ok) {
    const sent = Sent.safeParse(body);
    return sent.success
      ? { status: "sent", platform_ref: sent.data.messages[0].id }
      : failed("transient", "outcome_unknown");
  }
  const code = GraphError.safeParse(body).data?.error.code;
  if (response.status === 429 || (code !== undefined && THROTTLED.has(code)))
    return failed("rate_limited", "definite_not_sent");
  if (response.status >= 400 && response.status < 500)
    return failed("permanent", "definite_not_sent");
  return failed("transient", "outcome_unknown");
}

export type Sender = {
  readonly graphVersion: string;
  readonly fetch: Fetch;
  readonly now: () => number;
};

/**
 * WhatsApp has no idempotency key, so a send whose outcome is unknown parks.
 * The effect key rides in biz_opaque_callback_data, which Meta echoes on status webhooks.
 */
export function performer(sender: Sender): ChannelAdapter["perform"] {
  return async (raw, effectKey, credentials) => {
    const parsed = OutboundOp.safeParse(raw);
    if (!parsed.success) return failed("permanent", "definite_not_sent");
    const op = parsed.data;
    const since = op.last_inbound_at;
    if (since !== undefined && sender.now() - since > SESSION_WINDOW_MS)
      return failed("permanent", "definite_not_sent");
    const token = credentials["accessToken"];
    if (token === undefined)
      throw new Error("whatsapp: the host passed no accessToken");
    const url = `https://graph.facebook.com/${sender.graphVersion}/${op.installation_id}/messages`;
    let response: Response;
    try {
      response = await sandboxFetch(sender.fetch)(url, {
        method: "POST",
        headers: {
          authorization: `Bearer ${token}`,
          "content-type": "application/json",
        },
        body: JSON.stringify({
          messaging_product: "whatsapp",
          recipient_type: "individual",
          to: op.address,
          biz_opaque_callback_data: effectKey,
          ...message(op),
        }),
        signal: AbortSignal.timeout(30_000),
      });
    } catch (error) {
      if (error instanceof FenceRefused) throw error;
      return notConnected(error)
        ? failed("transient", "definite_not_sent")
        : failed("transient", "outcome_unknown");
    }
    return outcomeOf(response);
  };
}
