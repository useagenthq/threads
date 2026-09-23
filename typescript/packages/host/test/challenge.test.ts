import { describe, expect, test } from "bun:test";
import { secret, sqlite } from "@threads/core";
import { whatsapp } from "@threads/whatsapp";
import { host } from "../src";
import { fakeChannel, mailer } from "./kit";

// GET /channels/{channel}/events (openapi channelChallenge): the adapter's subscription check.

process.env["HOST_WA_VERIFY"] = "verify-me";
process.env["HOST_WA_APP"] = "app";
process.env["HOST_WA_TOKEN"] = "token";

function served() {
  return host({
    store: sqlite(":memory:"),
    agents: { support: mailer({ responses: [] }) },
    channels: {
      whatsapp: whatsapp({
        agent: "support",
        appSecret: secret("HOST_WA_APP"),
        accessToken: secret("HOST_WA_TOKEN"),
        verifyToken: secret("HOST_WA_VERIFY"),
      }),
      fake: fakeChannel("support"),
    },
  });
}

const get = (path: string): Request =>
  new Request(`http://host.test${path}`, { method: "GET" });

describe("channel subscription check", () => {
  test("the right token echoes the challenge; a wrong one is 401 unverified", async () => {
    const h = served();
    const ok = await h.fetch(
      get(
        "/channels/whatsapp/events?hub.mode=subscribe&hub.verify_token=verify-me&hub.challenge=42",
      ),
    );
    expect(ok.status).toBe(200);
    expect(await ok.text()).toBe("42");
    const bad = await h.fetch(
      get(
        "/channels/whatsapp/events?hub.mode=subscribe&hub.verify_token=nope&hub.challenge=42",
      ),
    );
    expect(bad.status).toBe(401);
    expect((await bad.json()).error.code).toBe("unverified");
  });

  test("a channel without a check, or an unknown channel, is not_found", async () => {
    const h = served();
    expect((await h.fetch(get("/channels/fake/events"))).status).toBe(404);
    expect((await h.fetch(get("/channels/nope/events"))).status).toBe(404);
  });
});
