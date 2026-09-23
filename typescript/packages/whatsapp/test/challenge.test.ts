import { describe, expect, test } from "bun:test";
import { secret } from "@threads/core/adapter";
import { whatsapp } from "../src";

// Meta's GET subscription check: the challenge is echoed only for the configured verify token.

process.env["WA_TEST_VERIFY_TOKEN"] = "verify-me";
process.env["WA_TEST_APP_SECRET_2"] = "app";
process.env["WA_TEST_TOKEN_2"] = "token";

function adapter(withToken = true) {
  return whatsapp({
    agent: "support",
    appSecret: secret("WA_TEST_APP_SECRET_2"),
    accessToken: secret("WA_TEST_TOKEN_2"),
    ...(withToken ? { verifyToken: secret("WA_TEST_VERIFY_TOKEN") } : {}),
  });
}

const good = {
  "hub.mode": "subscribe",
  "hub.verify_token": "verify-me",
  "hub.challenge": "1158201444",
};

describe("challenge", () => {
  test("the right token echoes hub.challenge", () => {
    const answer = adapter().challenge?.(good);
    expect(answer?.ok).toBe(true);
    if (answer?.ok !== true) return;
    expect(answer.value.status).toBe(200);
    expect(new TextDecoder().decode(answer.value.body)).toBe("1158201444");
  });

  test("a wrong or missing token, or another mode, is unverified", () => {
    const check = adapter().challenge;
    for (const query of [
      { ...good, "hub.verify_token": "verify-mE" },
      { ...good, "hub.verify_token": "verify-me-longer" },
      { "hub.mode": "subscribe", "hub.challenge": "1" },
      { ...good, "hub.mode": "unsubscribe" },
    ])
      expect(check?.(query)).toMatchObject({
        ok: false,
        error: { code: "unverified" },
      });
  });

  test("the answer never carries the token", () => {
    const answer = adapter().challenge?.({ ...good, "hub.verify_token": "x" });
    expect(JSON.stringify(answer)).not.toContain("verify-me");
  });

  test("without a verify token the adapter serves no check", () => {
    expect(adapter(false).challenge).toBeUndefined();
  });
});
