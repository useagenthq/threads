import { describe, expect, test } from "bun:test";
import { createHmac } from "node:crypto";
import type { RawRequest } from "@threads/core/adapter";
import { adapter, CHALLENGE, NOW_S, SIGNING } from "./kit";

// Inbound over raw bytes: signature and replay window, the workspace from the verified envelope,
// item keys per batch position, bots ignored, decisions only from challenge-bound buttons.

const utf8 = new TextEncoder();

function request(
  body: string,
  { ts = NOW_S, key = SIGNING, form = false } = {},
): RawRequest {
  const sig = createHmac("sha256", key)
    .update(`v0:${ts}:${body}`)
    .digest("hex");
  return {
    headers: {
      "x-slack-request-timestamp": String(ts),
      "x-slack-signature": `v0=${sig}`,
      "content-type": form
        ? "application/x-www-form-urlencoded"
        : "application/json",
    },
    body: utf8.encode(body),
  };
}

function eventBody(event: object, enterprise?: string): string {
  return JSON.stringify({
    type: "event_callback",
    team_id: "T1",
    enterprise_id: enterprise ?? null,
    event_id: "Ev1",
    event: { type: "message", channel: "C1", ts: "1.1", ...event },
  });
}

function actions(values: readonly string[]): RawRequest {
  const payload = JSON.stringify({
    type: "block_actions",
    team: { id: "T1" },
    user: { id: "U1" },
    trigger_id: "tr1",
    channel: { id: "C1" },
    message: { ts: "2.2", thread_ts: "1.1" },
    actions: values.map((value) => ({
      action_id: "a",
      value,
      action_ts: "3.3",
    })),
  });
  return request(new URLSearchParams({ payload }).toString(), { form: true });
}

const hello = request(eventBody({ user: "U1", text: "hello" }));

describe("verify", () => {
  test("a good signature yields the workspace tenant, installation and event id", () => {
    expect(adapter().verify(hello)).toEqual({
      ok: true,
      value: { tenant: "slack:T1", installation_id: "T1", delivery_id: "Ev1" },
    });
  });

  test("an enterprise workspace is qualified by its org", () => {
    const raw = request(eventBody({ user: "U1", text: "hi" }, "E1"));
    const v = adapter().verify(raw);
    expect(v.ok && v.value.installation_id).toBe("E1/T1");
  });

  test("a bad signature, a stale timestamp and a tampered body are unverified", () => {
    const body = eventBody({ user: "U1", text: "hi" });
    const tampered = {
      ...hello,
      body: utf8.encode(eventBody({ user: "U2", text: "hi" })),
    };
    for (const raw of [
      request(body, { key: "wrong" }),
      request(body, { ts: NOW_S - 301 }),
      tampered,
      { headers: {}, body: utf8.encode(body) },
    ]) {
      const v = adapter().verify(raw);
      expect(v.ok ? "ok" : v.error.code).toBe("unverified");
    }
  });

  test("an unknown workspace is unverified, and a tenant map routes known ones", () => {
    const map = (team: string) => (team === "T1" ? "acme" : undefined);
    const v = adapter({ tenant: map }).verify(hello);
    expect(v.ok && v.value.tenant).toBe("acme");
    const other = request(
      eventBody({ user: "U1", text: "hi" }).replace('"T1"', '"T9"'),
    );
    const refused = adapter({ tenant: map }).verify(other);
    expect(refused.ok ? "ok" : refused.error.code).toBe("unverified");
  });

  test("an interactive payload is keyed by its trigger id", () => {
    const v = adapter().verify(actions([]));
    expect(v.ok && v.value.delivery_id).toBe("tr1");
  });
});

describe("parse", () => {
  test("a message becomes one item keyed <event_id>#0 from the verified sender", () => {
    expect(adapter().parse(hello)).toEqual({
      ok: true,
      value: [
        {
          kind: "message",
          principal: { issuer: "slack:T1", tenant: "slack:T1", subject: "U1" },
          address: "C1",
          item_key: "Ev1#0",
          content: "hello",
        },
      ],
    });
  });

  test("a threaded message addresses its thread", () => {
    const p = adapter().parse(
      request(eventBody({ user: "U1", text: "x", thread_ts: "0.5" })),
    );
    expect(p.ok && p.value[0]).toMatchObject({ address: "C1:0.5" });
  });

  test("bot messages, the bot's own messages and edits are ignored", () => {
    for (const event of [
      { user: "U1", text: "x", bot_id: "B1" },
      { text: "x", subtype: "bot_message", bot_id: "B1" },
      { user: "UBOT", text: "x" },
      { user: "U1", subtype: "message_changed" },
    ])
      expect(adapter().parse(request(eventBody(event)))).toEqual({
        ok: true,
        value: [{ kind: "ignore" }],
      });
  });

  test("a challenge-bound button is a decision; any other button is ignored", () => {
    const grant = JSON.stringify({
      challenge_id: CHALLENGE,
      decision: "grant",
    });
    expect(adapter().parse(actions(["yes", grant]))).toEqual({
      ok: true,
      value: [
        { kind: "ignore" },
        {
          kind: "decision",
          principal: { issuer: "slack:T1", tenant: "slack:T1", subject: "U1" },
          address: "C1:1.1",
          item_key: "tr1#1",
          challenge_id: CHALLENGE,
          decision: "grant",
        },
      ],
    });
  });

  test("a malformed body is invalid", () => {
    for (const body of [
      "{not json",
      JSON.stringify({ type: "event_callback" }),
    ]) {
      const p = adapter().parse(request(body));
      expect(p.ok ? "ok" : p.error.code).toBe("invalid");
    }
  });
});

describe("url_verification", () => {
  const raw = request(
    JSON.stringify({ type: "url_verification", challenge: "abc", token: "t" }),
  );

  test("verifies, parses to nothing, and ack answers the challenge", () => {
    const v = adapter().verify(raw);
    expect(v.ok && v.value.delivery_id).toBe("url_verification");
    expect(adapter().parse(raw)).toEqual({ ok: true, value: [] });
    const res = adapter().ack(raw);
    expect(res.status).toBe(200);
    expect(JSON.parse(new TextDecoder().decode(res.body))).toEqual({
      challenge: "abc",
    });
  });

  test("any other request is acked with an empty 200", () => {
    expect(adapter().ack(hello)).toEqual({
      status: 200,
      headers: {},
      body: new Uint8Array(),
    });
  });
});
