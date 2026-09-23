import { LogLevel, WebAPIPlatformError, WebClient } from "@slack/web-api";
import {
  type ChannelAdapter,
  type DeliveryOutcome,
  FenceRefused,
  type Fetch,
  type LookupResult,
  responseText,
  sandboxFetch,
} from "@threads/core/adapter";
import { z } from "zod";
import type { ButtonValue } from "./inbound";

// Slack's outbound side: every op is one chat.postMessage carrying its effect
// key in message metadata, sent through the SDK's fetch behind the sandbox fence (// item 3) with the SDK's own retries off, so one perform is at most one transport attempt.

type Json = ReturnType<ChannelAdapter["render"]>[number];
type Event = Parameters<ChannelAdapter["render"]>[0];

const Op = z.discriminatedUnion("kind", [
  z.object({
    kind: z.literal("message"),
    address: z.string().min(1),
    text: z.string().min(1),
  }),
  z.object({
    kind: z.literal("approval"),
    address: z.string().min(1),
    challenge_id: z.string().min(1),
    text: z.string().min(1),
  }),
]);
type Op = z.infer<typeof Op>;

const Posted = z.object({ channel: z.string().min(1), ts: z.string().min(1) });
const Tagged = z.object({
  ts: z.string().min(1),
  metadata: z.object({
    event_type: z.literal("threads_effect"),
    event_payload: z.object({ effect_key: z.string() }),
  }),
});
const History = z.object({ messages: z.array(z.unknown()).default([]) });

/** Slack errors that may follow a partial success: the post is not proven absent. */
const AMBIGUOUS = new Set([
  "internal_error",
  "fatal_error",
  "request_timeout",
  "service_unavailable",
]);
/** Connect failures (Bun, Node): no request byte was written. */
const UNREACHED = new Set([
  "ConnectionRefused",
  "FailedToOpenSocket",
  "ECONNREFUSED",
  "ENOTFOUND",
  "EAI_AGAIN",
]);
const EVENT_TYPE = "threads_effect";

export function render(event: Event): readonly Json[] {
  if (event.type === "model_response") {
    const text = responseText(event.data.content);
    return text === "" ? [] : [{ kind: "message", text }];
  }
  if (event.type === "approval_requested")
    return [
      {
        kind: "approval",
        challenge_id: event.data.challenge_id,
        text: `Approval needed for call ${event.data.call_id}.`,
      },
    ];
  return [];
}

function button(challenge_id: string, decision: "grant" | "deny"): Json {
  const value: z.infer<typeof ButtonValue> = { challenge_id, decision };
  return {
    type: "button",
    text: {
      type: "plain_text",
      text: decision === "grant" ? "Approve" : "Deny",
    },
    style: decision === "grant" ? "primary" : "danger",
    action_id: `threads_${decision}`,
    value: JSON.stringify(value),
  };
}

function blocks(op: Op): readonly Json[] | undefined {
  if (op.kind === "message") return undefined;
  return [
    { type: "section", text: { type: "mrkdwn", text: op.text } },
    {
      type: "actions",
      elements: [
        button(op.challenge_id, "grant"),
        button(op.challenge_id, "deny"),
      ],
    },
  ];
}

/** `<channel>` or `<channel>:<thread_ts>`; Slack ids never contain a colon. */
function where(address: string): { channel: string; thread_ts?: string } {
  const [channel = address, thread_ts] = address.split(":", 2);
  return thread_ts === undefined ? { channel } : { channel, thread_ts };
}

/** A client whose every request leaves through the fence; `seen` records the HTTP status. */
function client(
  token: string,
  transport: Fetch,
  seen: { status?: number },
): WebClient {
  const observed: Fetch = async (input, init) => {
    const response = await transport(input, init);
    seen.status = response.status;
    return response;
  };
  return new WebClient(token, {
    fetch: sandboxFetch(observed),
    retryConfig: { retries: 0 },
    rejectRateLimitedCalls: true,
    timeout: 30_000,
    logLevel: LogLevel.ERROR,
  });
}

function* chain(error: unknown): Generator<unknown> {
  for (
    let e = error;
    e !== undefined && e !== null;
    e = e instanceof Error ? e.cause : undefined
  )
    yield e;
}

/** A fence refusal is the host's to report (within()), never a delivery outcome. */
function rethrowFence(error: unknown): void {
  for (const e of chain(error)) if (e instanceof FenceRefused) throw e;
}

const failed = (
  kind: "rate_limited" | "transient" | "permanent",
  sent: "definite_not_sent" | "outcome_unknown",
): DeliveryOutcome => ({ status: "delivery_error", kind, sent });

function outcome(error: unknown, status: number | undefined): DeliveryOutcome {
  rethrowFence(error);
  if (status === 429) return failed("rate_limited", "definite_not_sent");
  if (error instanceof WebAPIPlatformError)
    return AMBIGUOUS.has(error.data.error)
      ? failed("transient", "outcome_unknown")
      : failed("permanent", "definite_not_sent");
  const unreached = [...chain(error)].some(
    (e) => e instanceof Error && "code" in e && UNREACHED.has(String(e.code)),
  );
  return status === undefined && unreached
    ? failed("transient", "definite_not_sent")
    : failed("transient", "outcome_unknown");
}

export function performer(transport: Fetch): ChannelAdapter["perform"] {
  return async (raw, effectKey, credentials) => {
    const op = Op.safeParse(raw);
    const token = credentials["botToken"];
    if (!op.success || token === undefined)
      return failed("permanent", "definite_not_sent");
    const seen: { status?: number } = {};
    const at = where(op.data.address);
    const extra = blocks(op.data);
    try {
      const result = await client(token, transport, seen).chat.postMessage({
        ...at,
        text: op.data.text,
        ...(extra === undefined ? {} : { blocks: [...extra] }),
        metadata: {
          event_type: EVENT_TYPE,
          event_payload: { effect_key: effectKey },
        },
      });
      const posted = Posted.safeParse(result);
      return posted.success
        ? {
            status: "sent",
            platform_ref: `${posted.data.channel}:${posted.data.ts}`,
          }
        : failed("transient", "outcome_unknown");
    } catch (error) {
      return outcome(error, seen.status);
    }
  };
}

// ponytail: scans the newest 200 messages of the conversation. Absence there never proves the
// post is absent (older, deleted, or paged out), so lookup is nonfinal.
export function lookuper(
  transport: Fetch,
  token: () => string,
): ChannelAdapter["lookup"] {
  return async (effectKey, raw): Promise<LookupResult<string>> => {
    const op = Op.safeParse(raw);
    if (!op.success) return { status: "unknown", reason: "invalid op" };
    const { channel, thread_ts } = where(op.data.address);
    const slack = client(token(), transport, {});
    const query = { channel, include_all_metadata: true, limit: 200 };
    try {
      const page = History.parse(
        thread_ts === undefined
          ? await slack.conversations.history(query)
          : await slack.conversations.replies({ ...query, ts: thread_ts }),
      );
      for (const message of page.messages) {
        const m = Tagged.safeParse(message);
        if (m.success && m.data.metadata.event_payload.effect_key === effectKey)
          return { status: "found", value: `${channel}:${m.data.ts}` };
      }
      return { status: "not_found_nonfinal" };
    } catch (error) {
      rethrowFence(error);
      const reason =
        error instanceof WebAPIPlatformError
          ? error.data.error
          : "request failed";
      return { status: "unknown", reason: `slack lookup: ${reason}` };
    }
  };
}
