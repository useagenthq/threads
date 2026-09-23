import { describe, expect, test } from "bun:test";
import { secret, within } from "@threads/core/adapter";
import { CTX } from "../../core/test/sandbox/context";
import { unwrap } from "../../core/test/store/helpers";
import { whatsapp } from "../src";

// Live gate: the real Graph API, so it runs only with THREADS_LIVE=1 and a
// token, a business phone number id and a test recipient who wrote to it in the last 24 hours.

const token = process.env["WHATSAPP_ACCESS_TOKEN"];
const phone = process.env["WHATSAPP_PHONE_NUMBER_ID"];
const recipient = process.env["WHATSAPP_TEST_RECIPIENT"];
const live =
  process.env["THREADS_LIVE"] === "1" &&
  token !== undefined &&
  phone !== undefined &&
  recipient !== undefined;

describe.skipIf(!live)("live gate: whatsapp", () => {
  test("a text send returns a wamid", async () => {
    const wa = whatsapp({
      agent: "live",
      appSecret: secret("WHATSAPP_APP_SECRET"),
      accessToken: secret("WHATSAPP_ACCESS_TOKEN"),
    });
    const credentials = { accessToken: token ?? "" };
    const op = {
      kind: "text",
      text: "threads live gate",
      address: recipient ?? "",
      installation_id: phone ?? "",
    };
    const key = `live:${crypto.randomUUID()}`;
    const sent = unwrap(
      await within(CTX, () => wa.perform(op, key, credentials)),
    );
    expect(sent).toMatchObject({ status: "sent" });
  });
});
