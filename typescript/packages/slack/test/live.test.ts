import { describe, expect, test } from "bun:test";
import { secret, within } from "@threads/core/adapter";
import { CTX } from "../../core/test/sandbox/context";
import { slack } from "../src";

// Live gate: real Slack, so it runs only with THREADS_LIVE=1, SLACK_BOT_TOKEN and
// SLACK_TEST_CHANNEL. Posts one message, then finds it by its effect key.

const token = process.env["SLACK_BOT_TOKEN"];
const channel = process.env["SLACK_TEST_CHANNEL"];
const live =
  process.env["THREADS_LIVE"] === "1" &&
  token !== undefined &&
  channel !== undefined;

describe.skipIf(!live)("live gate: slack", () => {
  test("perform posts with the effect key and lookup finds it", async () => {
    const adapter = slack({
      agent: "live",
      signingSecret: secret("SLACK_SIGNING_SECRET"),
      botToken: secret("SLACK_BOT_TOKEN"),
    });
    const key = `live:${crypto.randomUUID()}`;
    const op = {
      kind: "message",
      address: channel ?? "",
      text: `threads live ${key}`,
    };
    const sent = await within(CTX, () =>
      adapter.perform(op, key, { botToken: token ?? "" }),
    );
    expect(sent.ok && sent.value.status).toBe("sent");
    const found = await within(CTX, () => adapter.lookup(key, op));
    expect(found.ok && found.value.status).toBe("found");
  });
});
