import { describe, expect, test } from "bun:test";
import { secret } from "@threads/core/adapter";
import { unwrap } from "../../core/test/store/helpers";
import { whatsapp } from "../src";
import {
  adapter,
  batch,
  CHALLENGE,
  PHONE,
  request,
  utf8,
  webhook,
} from "./fixtures";

describe("verify", () => {
  test("a good signature names the phone number as the installation", () => {
    const verified = unwrap(adapter().verify(request(batch)));
    expect(verified.tenant).toBe(`whatsapp:${PHONE}`);
    expect(verified.installation_id).toBe(PHONE);
    expect(verified.delivery_id).toMatch(/^[0-9a-f]{64}$/);
    expect(unwrap(adapter().verify(request(batch))).delivery_id).toBe(
      verified.delivery_id,
    );
  });

  test("a bad or missing signature is unverified", () => {
    expect(adapter().verify(request(batch, "wrong")).ok).toBe(false);
    const raw = request(batch);
    expect(adapter().verify({ headers: {}, body: raw.body }).ok).toBe(false);
  });

  test("a phone number with no tenant is unverified", () => {
    const only = whatsapp({
      agent: "support",
      appSecret: secret("WA_TEST_APP_SECRET"),
      accessToken: secret("WA_TEST_ACCESS_TOKEN"),
      tenant: (id) => (id === "other" ? "acme" : undefined),
    });
    expect(only.verify(request(batch)).ok).toBe(false);
    expect(unwrap(only.verify(request(webhook({}, "other")))).tenant).toBe(
      "acme",
    );
  });

  test("a batch spanning two phone numbers is unverified", () => {
    const mixed = {
      ...webhook({}),
      entry: [...webhook({}).entry, ...webhook({}, "999").entry],
    };
    expect(adapter().verify(request(mixed)).ok).toBe(false);
  });
});

describe("parse", () => {
  test("a batch of two messages is two items keyed by their ids", () => {
    const items = unwrap(adapter().parse(request(batch)));
    const principal = {
      issuer: `whatsapp:${PHONE}`,
      tenant: `whatsapp:${PHONE}`,
      subject: "15551234567",
    };
    expect(items).toEqual([
      {
        kind: "message",
        principal,
        address: "15551234567",
        item_key: "wamid.A",
        content: "hello",
      },
      {
        kind: "message",
        principal,
        address: "15551234567",
        item_key: "wamid.B",
        content: "again",
      },
    ]);
  });

  test("delivery statuses and unsupported types are ignored", () => {
    const raw = request(
      webhook({
        statuses: [{ id: "wamid.X", status: "delivered", recipient_id: "1" }],
        messages: [
          {
            from: "1",
            id: "wamid.S",
            timestamp: "1",
            type: "sticker",
            sticker: {},
          },
        ],
      }),
    );
    expect(unwrap(adapter().parse(raw))).toEqual([
      { kind: "ignore" },
      { kind: "ignore" },
    ]);
  });

  test("an approval button reply is a decision; a free-text yes is not", () => {
    const reply = (id: string, wamid: string) => ({
      from: "15551234567",
      id: wamid,
      timestamp: "1",
      type: "interactive",
      interactive: {
        type: "button_reply",
        button_reply: { id, title: "Approve" },
      },
    });
    const json = JSON.stringify({ challenge_id: CHALLENGE, decision: "deny" });
    const items = unwrap(
      adapter().parse(
        request(
          webhook({
            messages: [
              reply(`approve:${CHALLENGE}`, "wamid.1"),
              reply(json, "wamid.2"),
              reply("approve:not-a-uuid", "wamid.3"),
            ],
          }),
        ),
      ),
    );
    expect(
      items.map((i) => (i.kind === "decision" ? i.decision : i.kind)),
    ).toEqual(["grant", "deny", "ignore"]);
    expect(items[0]).toMatchObject({
      challenge_id: CHALLENGE,
      item_key: "wamid.1",
    });
  });

  test("a malformed body is invalid", () => {
    expect(
      adapter().parse({ headers: {}, body: utf8.encode("{nope") }).ok,
    ).toBe(false);
    expect(adapter().parse(request({ object: "x", entry: "y" })).ok).toBe(
      false,
    );
  });

  test("ack is an empty 200", () => {
    const ack = adapter().ack(request(batch));
    expect(ack.status).toBe(200);
    expect(ack.body.length).toBe(0);
  });
});
