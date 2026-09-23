import { createHmac, timingSafeEqual } from "node:crypto";
import {
  assertNever,
  type ChannelAdapter,
  Inbound,
  type RawRequest,
  type RawResponse,
  type Secret,
} from "@threads/core/adapter";
import { z } from "zod";

// Slack's inbound side: the signing secret
// and a 5-minute timestamp window over the raw bytes, the workspace from the verified envelope
// only, and one item key per batch position.

export type Tenancy = string | ((teamId: string) => string | undefined);

type Verify = ReturnType<ChannelAdapter["verify"]>;
type Parse = ReturnType<ChannelAdapter["parse"]>;

const WINDOW_S = 300;
const utf8 = new TextDecoder();
const encode = new TextEncoder();

const Message = z.object({
  type: z.literal("message"),
  subtype: z.string().optional(),
  bot_id: z.string().optional(),
  user: z.string().optional(),
  text: z.string().optional(),
  channel: z.string().min(1),
  thread_ts: z.string().optional(),
});

const Envelope = z.discriminatedUnion("type", [
  z.object({ type: z.literal("url_verification"), challenge: z.string() }),
  z.object({
    type: z.literal("event_callback"),
    team_id: z.string().min(1),
    enterprise_id: z.string().nullish(),
    event_id: z.string().min(1),
    event: z.object({ type: z.string() }).loose(),
  }),
  z.object({
    type: z.literal("block_actions"),
    team: z.object({
      id: z.string().min(1),
      enterprise_id: z.string().nullish(),
    }),
    user: z.object({ id: z.string().min(1) }),
    trigger_id: z.string().min(1),
    channel: z.object({ id: z.string().min(1) }),
    message: z.object({ thread_ts: z.string().optional() }).optional(),
    actions: z.array(z.object({ value: z.string().optional() })),
  }),
]);
type Envelope = z.infer<typeof Envelope>;

/** Button values carry only the challenge id and the decision. */
export const ButtonValue: z.ZodObject<{
  challenge_id: z.ZodString;
  decision: z.ZodEnum<{ grant: "grant"; deny: "deny" }>;
}> = z.object({
  challenge_id: z.string(),
  decision: z.enum(["grant", "deny"]),
});

/** The body as JSON: interactive payloads arrive form-encoded under `payload`. */
function envelope(raw: RawRequest): Envelope | undefined {
  const text = utf8.decode(raw.body);
  const form = raw.headers["content-type"]?.startsWith(
    "application/x-www-form-urlencoded",
  );
  const json = form ? new URLSearchParams(text).get("payload") : text;
  if (json === null || json === undefined) return undefined;
  try {
    const parsed = Envelope.safeParse(JSON.parse(json));
    return parsed.success ? parsed.data : undefined;
  } catch {
    return undefined;
  }
}

function signed(
  raw: RawRequest,
  signingSecret: Secret,
  nowMs: number,
): string | undefined {
  const ts = raw.headers["x-slack-request-timestamp"] ?? "";
  const given = raw.headers["x-slack-signature"] ?? "";
  if (!/^\d+$/.test(ts)) return "missing timestamp";
  if (Math.abs(nowMs / 1000 - Number(ts)) > WINDOW_S)
    return "timestamp outside the replay window";
  const mac = createHmac("sha256", signingSecret.reveal())
    .update(encode.encode(`v0:${ts}:`))
    .update(raw.body)
    .digest("hex");
  const want = Buffer.from(`v0=${mac}`);
  const got = Buffer.from(given);
  return want.length === got.length && timingSafeEqual(want, got)
    ? undefined
    : "bad signature";
}

type Workspace = {
  readonly team: string;
  readonly enterprise?: string | null | undefined;
};

function workspace(body: Envelope): Workspace | undefined {
  switch (body.type) {
    case "url_verification":
      return undefined;
    case "event_callback":
      return { team: body.team_id, enterprise: body.enterprise_id };
    case "block_actions":
      return { team: body.team.id, enterprise: body.team.enterprise_id };
    default:
      return assertNever(body);
  }
}

function tenantOf(
  tenancy: Tenancy | undefined,
  team: string,
): string | undefined {
  if (tenancy === undefined) return `slack:${team}`;
  return typeof tenancy === "string" ? tenancy : tenancy(team);
}

const unverified = (message: string): Verify => ({
  ok: false,
  error: { code: "unverified", message },
});

export function verifier(
  signingSecret: Secret,
  tenancy: Tenancy | undefined,
  now: () => number,
): ChannelAdapter["verify"] {
  return (raw) => {
    const bad = signed(raw, signingSecret, now());
    if (bad !== undefined) return unverified(bad);
    const body = envelope(raw);
    if (body === undefined) return unverified("unrecognized Slack request");
    if (body.type === "url_verification")
      return {
        ok: true,
        value: {
          tenant: "slack",
          installation_id: "slack",
          delivery_id: "url_verification",
        },
      };
    const ws = workspace(body);
    if (ws === undefined) return unverified("no workspace");
    const tenant = tenantOf(tenancy, ws.team);
    if (tenant === undefined) return unverified(`unknown workspace ${ws.team}`);
    return {
      ok: true,
      value: {
        tenant,
        // Enterprise Grid: the org qualifies the workspace, so E1/T1 and E2/T1 never collide.
        installation_id: ws.enterprise
          ? `${ws.enterprise}/${ws.team}`
          : ws.team,
        delivery_id:
          body.type === "event_callback" ? body.event_id : body.trigger_id,
      },
    };
  };
}

function messageItem(
  body: Extract<Envelope, { type: "event_callback" }>,
  tenant: string,
  botUserId: string | undefined,
): Inbound {
  const m = Message.safeParse(body.event);
  if (!m.success) return { kind: "ignore" };
  const { subtype, bot_id, user, text, channel, thread_ts } = m.data;
  const own = user === undefined || user === botUserId;
  if (subtype !== undefined || bot_id !== undefined || own || !text)
    return { kind: "ignore" };
  return {
    kind: "message",
    principal: { issuer: `slack:${body.team_id}`, tenant, subject: user },
    address: thread_ts ? `${channel}:${thread_ts}` : channel,
    item_key: `${body.event_id}#0`,
    content: text,
  };
}

function decisionItems(
  body: Extract<Envelope, { type: "block_actions" }>,
  tenant: string,
): readonly Inbound[] {
  const thread = body.message?.thread_ts;
  const address = thread ? `${body.channel.id}:${thread}` : body.channel.id;
  const principal = {
    issuer: `slack:${body.team.id}`,
    tenant,
    subject: body.user.id,
  };
  return body.actions.map((action, index): Inbound => {
    const value = ButtonValue.safeParse(tryJson(action.value));
    if (!value.success) return { kind: "ignore" };
    const item = Inbound.safeParse({
      kind: "decision",
      principal,
      address,
      item_key: `${body.trigger_id}#${index}`,
      ...value.data,
    });
    return item.success ? item.data : { kind: "ignore" };
  });
}

function tryJson(text: string | undefined): unknown {
  try {
    return text === undefined ? undefined : JSON.parse(text);
  } catch {
    return undefined;
  }
}

export function parser(
  tenancy: Tenancy | undefined,
  botUserId: string | undefined,
): ChannelAdapter["parse"] {
  return (raw): Parse => {
    const body = envelope(raw);
    if (body === undefined)
      return {
        ok: false,
        error: { code: "invalid", message: "malformed Slack body" },
      };
    const ws = workspace(body);
    const tenant = ws === undefined ? undefined : tenantOf(tenancy, ws.team);
    if (body.type === "url_verification" || tenant === undefined)
      return { ok: true, value: [] };
    if (body.type === "event_callback")
      return { ok: true, value: [messageItem(body, tenant, botUserId)] };
    return { ok: true, value: decisionItems(body, tenant) };
  };
}

export function ack(raw: RawRequest): RawResponse {
  const body = envelope(raw);
  if (body?.type !== "url_verification")
    return { status: 200, headers: {}, body: new Uint8Array() };
  return {
    status: 200,
    headers: { "content-type": "application/json" },
    body: encode.encode(JSON.stringify({ challenge: body.challenge })),
  };
}
