import { createHash, createHmac, timingSafeEqual } from "node:crypto";
import type {
  ChannelAdapter,
  Fetch,
  RawRequest,
  Secret,
  VerifiedDelivery,
} from "@threads/core/adapter";
import { itemsOf, phoneNumbersOf } from "./inbound";
import { performer, render, SESSION_WINDOW_MS } from "./outbound";

// whatsapp(): the WhatsApp Cloud API as a threads channel (spec/api.json ChannelAdapter,
// ). What it declares, and why:
// - Authenticity: X-Hub-Signature-256 over the raw body. Meta signs no timestamp, so replay
//   protection is the host inbox's dedup on each messages[].id.
// - Installation: the business phone number id. A webhook that speaks for two phone numbers is
//   refused rather than filed under one of them.
// - Lookup: none. Meta offers no way to find a sent message by our key, so an unknown send parks.
// - The 24-hour session window: outside it a free-form send fails before any request.

export type WhatsappOptions = {
  /** The host agent key this channel routes to. */
  readonly agent: string;
  /** The Meta app secret that signs webhooks. */
  readonly appSecret: Secret;
  /** A system-user access token; the host resolves it and passes it to perform only. */
  readonly accessToken: Secret;
  /** Graph API version. Defaults to "v21.0". */
  readonly graphVersion?: string;
  /** The tenant for a phone number id; undefined refuses it. Defaults to `whatsapp:<id>`. */
  readonly tenant?: string | ((phoneNumberId: string) => string | undefined);
  /** The transport under the fence. Defaults to the process's fetch. */
  readonly fetch?: Fetch;
  readonly now?: () => number;
};

type Verified = ReturnType<ChannelAdapter["verify"]>;

function signed(raw: RawRequest, appSecret: Secret): boolean {
  const header = raw.headers["x-hub-signature-256"] ?? "";
  const match = /^sha256=([0-9a-f]{64})$/.exec(header);
  if (match?.[1] === undefined) return false;
  const expected = createHmac("sha256", appSecret.reveal())
    .update(raw.body)
    .digest();
  return timingSafeEqual(Buffer.from(match[1], "hex"), expected);
}

export function whatsapp(options: WhatsappOptions): ChannelAdapter {
  const { tenant } = options;
  const tenantOf = (id: string): string | undefined =>
    typeof tenant === "function" ? tenant(id) : (tenant ?? `whatsapp:${id}`);
  const unverified = (message: string): Verified => ({
    ok: false,
    error: { code: "unverified", message },
  });

  const verify = (raw: RawRequest): Verified => {
    if (!signed(raw, options.appSecret))
      return unverified("bad or missing X-Hub-Signature-256");
    const phones = phoneNumbersOf(raw.body);
    const [phone] = phones;
    if (phone === undefined || phones.length > 1)
      return unverified("the webhook must speak for exactly one phone number");
    const scope = tenantOf(phone);
    if (scope === undefined)
      return unverified(`phone number ${phone} has no tenant`);
    // Meta sends no delivery id. The hash of the signed bytes is the same when Meta redelivers
    // those bytes; if it re-batches, dedup still holds because items are keyed by messages[].id.
    const delivery_id = createHash("sha256").update(raw.body).digest("hex");
    return {
      ok: true,
      value: {
        tenant: scope,
        installation_id: phone,
        delivery_id,
      } satisfies VerifiedDelivery,
    };
  };

  return {
    agent: options.agent,
    capabilities: {
      lookup: "none",
      buttons: true,
      edits: false,
      files: true,
      direct_messages: true,
      delivery: "reliable",
    },
    limits: { text_bytes: 4096, session_window_ms: SESSION_WINDOW_MS },
    secrets: { accessToken: options.accessToken },
    verify,
    parse: (raw) => {
      const items = itemsOf(raw.body, tenantOf);
      return items === undefined
        ? {
            ok: false,
            error: {
              code: "invalid",
              message: "not a WhatsApp webhook of a known tenant",
            },
          }
        : { ok: true, value: items };
    },
    ack: () => ({ status: 200, headers: {}, body: new Uint8Array() }),
    render,
    perform: performer({
      graphVersion: options.graphVersion ?? "v21.0",
      fetch: options.fetch ?? ((input, init) => fetch(input, init)),
      now: options.now ?? Date.now,
    }),
    lookup: async () => ({
      status: "unknown",
      reason: "whatsapp has no delivery lookup",
    }),
  };
}
