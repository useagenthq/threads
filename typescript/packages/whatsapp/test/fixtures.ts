import { createHmac } from "node:crypto";
import {
  type ChannelAdapter,
  type Fetch,
  type RawRequest,
  secret,
} from "@threads/core/adapter";
import { whatsapp } from "../src";

// Shared fixtures for the whatsapp tests: a signed Cloud API webhook and a fake fetch.

export const APP_SECRET = "app-secret-for-tests";
export const TOKEN = "EAAG-token-never-leaks";
process.env["WA_TEST_APP_SECRET"] = APP_SECRET;

export const PHONE = "106540352242922";
export const CHALLENGE = "0192c000-0000-7000-8000-000000000001";
export const NOW = 1_790_000_000_000;
export const utf8: TextEncoder = new TextEncoder();

export type Call = {
  readonly url: string;
  readonly init: RequestInit | undefined;
};

export function fakeFetch(answer: () => Response): {
  readonly fetch: Fetch;
  readonly calls: Call[];
} {
  const calls: Call[] = [];
  const fetch: Fetch = async (input, init) => {
    calls.push({ url: String(input), init });
    return answer();
  };
  return { fetch, calls };
}

export function adapter(
  fetch: Fetch = fakeFetch(() => Response.json({})).fetch,
): ChannelAdapter {
  return whatsapp({
    agent: "support",
    appSecret: secret("WA_TEST_APP_SECRET"),
    accessToken: secret("WA_TEST_ACCESS_TOKEN"),
    verifyToken: secret("WA_TEST_VERIFY_TOKEN"),
    fetch,
    now: () => NOW,
  });
}

export function request(
  payload: unknown,
  sign: string = APP_SECRET,
): RawRequest {
  const body = utf8.encode(JSON.stringify(payload));
  const hex = createHmac("sha256", sign).update(body).digest("hex");
  return { headers: { "x-hub-signature-256": `sha256=${hex}` }, body };
}

const text = (id: string, body: string) => ({
  from: "15551234567",
  id,
  timestamp: "1790000000",
  type: "text",
  text: { body },
});

type Webhook = { readonly object: string; readonly entry: readonly unknown[] };

export function webhook(
  value: Record<string, unknown>,
  phone: string = PHONE,
): Webhook {
  return {
    object: "whatsapp_business_account",
    entry: [
      {
        id: "WABA_1",
        changes: [
          {
            field: "messages",
            value: {
              messaging_product: "whatsapp",
              metadata: {
                display_phone_number: "15550000000",
                phone_number_id: phone,
              },
              ...value,
            },
          },
        ],
      },
    ],
  };
}

export const batch: Webhook = webhook({
  messages: [text("wamid.A", "hello"), text("wamid.B", "again")],
});
