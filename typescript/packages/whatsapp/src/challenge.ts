import { createHash, timingSafeEqual } from "node:crypto";
import type { ChannelAdapter, Secret } from "@threads/core/adapter";

// Meta's webhook subscription check (spec/api.json ChannelAdapter.challenge): a GET with
// hub.mode=subscribe, hub.verify_token and hub.challenge. The token is compared with the
// configured secret in constant time; only then is the challenge echoed.

type Challenge = NonNullable<ChannelAdapter["challenge"]>;

/** Equal-length digests, so the comparison leaks neither content nor length. */
function same(a: string, b: string): boolean {
  const digest = (s: string): Buffer => createHash("sha256").update(s).digest();
  return timingSafeEqual(digest(a), digest(b));
}

export function challenger(verifyToken: Secret): Challenge {
  return (query) => {
    const token = query["hub.verify_token"] ?? "";
    const echo = query["hub.challenge"];
    if (
      query["hub.mode"] !== "subscribe" ||
      echo === undefined ||
      !same(token, verifyToken.reveal())
    )
      return {
        ok: false,
        error: { code: "unverified", message: "bad hub.verify_token" },
      };
    return {
      ok: true,
      value: {
        status: 200,
        headers: { "content-type": "text/plain" },
        body: new TextEncoder().encode(echo),
      },
    };
  };
}
