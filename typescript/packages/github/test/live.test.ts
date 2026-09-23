import { describe, expect, test } from "bun:test";
import { secret, within } from "@threads/core/adapter";
import { CTX } from "../../core/test/sandbox/context";
import { github } from "../src";

// Live gate: real GitHub, so it runs only with THREADS_LIVE=1, GITHUB_TOKEN
// and GITHUB_TEST_ISSUE (`owner/repo#number`, an issue the token may comment on).

const token = process.env["GITHUB_TOKEN"];
const issue = process.env["GITHUB_TEST_ISSUE"];
const live =
  process.env["THREADS_LIVE"] === "1" &&
  token !== undefined &&
  issue !== undefined;

describe.skipIf(!live)("live gate: github", () => {
  test("a comment is posted with its marker and found by lookup", async () => {
    process.env["THREADS_GH_LIVE_WEBHOOK"] = "unused";
    const adapter = github({
      agent: "live",
      webhookSecret: secret("THREADS_GH_LIVE_WEBHOOK"),
      token: secret("GITHUB_TOKEN"),
    });
    const op = {
      kind: "comment",
      text: "threads live test",
      address: issue ?? "",
      installation_id: "live",
    };
    const key = `live:${crypto.randomUUID()}`;
    const sent = await within(CTX, () =>
      adapter.perform(op, key, { token: token ?? "" }),
    );
    expect(sent).toMatchObject({ ok: true, value: { status: "sent" } });
    const found = await within(CTX, () => adapter.lookup(key, op));
    expect(found).toMatchObject({ ok: true, value: { status: "found" } });
  });
});
