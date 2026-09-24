import { describe, expect, test } from "bun:test";
import {
  type ChannelAdapter,
  type DeliveryOutcome,
  type Fetch,
  within,
} from "@threads/core/adapter";
import { KnownEvent } from "../../core/src/log";
import { err } from "../../core/src/result";
import type { SandboxContext } from "../../core/src/sandbox";
import { CTX } from "../../core/test/sandbox/context";
import { unwrap } from "../../core/test/store/helpers";
import {
  adapter,
  CHALLENGE,
  fakeFetch,
  NOW,
  PHONE,
  request,
  TOKEN,
  webhook,
} from "./fixtures";

const envelope = {
  seq: 5,
  event_id: "0192d000-0000-7000-8000-000000000001",
  thread_id: "0192a000-0000-7000-8000-000000000001",
  branch_id: "0192b000-0000-7000-8000-000000000001",
  epoch: 1,
  type_version: 1,
  time: NOW,
  actor: { kind: "model" },
  prev_hash: "0".repeat(64),
  critical: true,
};

describe("render", () => {
  test("a model response is one text op; an approval is a button op", () => {
    const response = KnownEvent.parse({
      ...envelope,
      type: "model_response",
      data: {
        request_event_id: envelope.event_id,
        content: [
          { type: "text", text: "Hi " },
          { type: "text", text: "there" },
        ],
        stop_reason: "end_turn",
        usage: { input_tokens: 1, output_tokens: 1 },
        completeness: "complete",
      },
    });
    expect(adapter().render(response)).toEqual([
      { kind: "text", text: "Hi there" },
    ]);
    const approval = KnownEvent.parse({
      ...envelope,
      type: "approval_requested",
      actor: { kind: "host" },
      data: {
        challenge_id: CHALLENGE,
        call_id: "call_1",
        args_hash: "a".repeat(64),
        expires_at: NOW + 1,
      },
    });
    expect(adapter().render(approval)).toEqual([
      {
        kind: "approval",
        challenge_id: CHALLENGE,
        text: expect.stringContaining("call_1"),
      },
    ]);
  });
});

const op = {
  kind: "text",
  text: "hi",
  address: "15551234567",
  installation_id: PHONE,
};
const KEY = "0192b000-0000-7000-8000-000000000001:send_4_0";

async function perform(
  fetch: Fetch,
  sent: Parameters<ChannelAdapter["perform"]>[0] = op,
  context: SandboxContext = CTX,
) {
  const done = await within(context, () =>
    adapter(fetch).perform(sent, KEY, { accessToken: TOKEN }),
  );
  return done;
}

describe("perform", () => {
  test("a reply is sent from the phone number its message arrived on", async () => {
    for (const phone of ["PHONE_A", "PHONE_B"]) {
      const fake = fakeFetch(() =>
        Response.json({ messages: [{ id: "wamid.OUT" }] }),
      );
      const verified = unwrap(
        adapter(fake.fetch).verify(request(webhook({}, phone))),
      );
      await perform(fake.fetch, {
        ...op,
        installation_id: verified.installation_id,
      });
      expect(fake.calls[0]?.url).toBe(
        `https://graph.facebook.com/v21.0/${phone}/messages`,
      );
    }
  });

  test("a 200 with a message id is sent, with the effect key in the callback data", async () => {
    const fake = fakeFetch(() =>
      Response.json({ messages: [{ id: "wamid.OUT" }] }),
    );
    const done = unwrap(await perform(fake.fetch));
    expect(done).toEqual({ status: "sent", platform_ref: "wamid.OUT" });
    const call = fake.calls[0];
    expect(call?.url).toBe(
      `https://graph.facebook.com/v21.0/${PHONE}/messages`,
    );
    const body = JSON.parse(String(call?.init?.body));
    expect(body).toMatchObject({
      to: "15551234567",
      type: "text",
      text: { body: "hi" },
      biz_opaque_callback_data: KEY,
    });
    expect(new Headers(call?.init?.headers).get("authorization")).toBe(
      `Bearer ${TOKEN}`,
    );
    expect(JSON.stringify(done)).not.toContain(TOKEN);
  });

  test("an approval op is an interactive message carrying only the challenge id", async () => {
    const fake = fakeFetch(() =>
      Response.json({ messages: [{ id: "wamid.OUT" }] }),
    );
    const approval = {
      ...op,
      kind: "approval",
      challenge_id: CHALLENGE,
      text: "ok?",
    };
    unwrap(await perform(fake.fetch, approval));
    const body = JSON.parse(String(fake.calls[0]?.init?.body));
    const ids = body.interactive.action.buttons.map(
      (b: { reply: { id: string } }) => b.reply.id,
    );
    expect(ids).toEqual([`approve:${CHALLENGE}`, `deny:${CHALLENGE}`]);
  });

  const failed = (
    kind: "rate_limited" | "transient" | "permanent",
    sent: "definite_not_sent" | "outcome_unknown",
  ): DeliveryOutcome => ({ status: "delivery_error", kind, sent });
  const cases: [string, () => Response, DeliveryOutcome][] = [
    [
      "a Graph 4xx is permanent, not sent",
      () =>
        Response.json(
          { error: { code: 131047, message: "re-engage" } },
          { status: 400 },
        ),
      failed("permanent", "definite_not_sent"),
    ],
    [
      "a 429 is rate limited, not sent",
      () => new Response("", { status: 429 }),
      failed("rate_limited", "definite_not_sent"),
    ],
    [
      "a throttling code is rate limited, not sent",
      () => Response.json({ error: { code: 131056 } }, { status: 400 }),
      failed("rate_limited", "definite_not_sent"),
    ],
    [
      "a 5xx is transient with an unknown outcome",
      () => new Response("", { status: 502 }),
      failed("transient", "outcome_unknown"),
    ],
    [
      "a refused connection is transient, not sent",
      () => {
        throw Object.assign(new Error("Unable to connect"), {
          code: "ConnectionRefused",
        });
      },
      failed("transient", "definite_not_sent"),
    ],
    [
      "a reset or timeout is transient with an unknown outcome",
      () => {
        throw new DOMException("timed out", "TimeoutError");
      },
      failed("transient", "outcome_unknown"),
    ],
  ];
  for (const [name, answer, outcome] of cases)
    test(name, async () => {
      const done = unwrap(await perform(fakeFetch(answer).fetch));
      expect(done).toEqual(outcome);
      expect(JSON.stringify(done)).not.toContain(TOKEN);
    });

  test("outside the 24-hour window nothing is requested", async () => {
    const fake = fakeFetch(() => Response.json({ messages: [{ id: "x" }] }));
    const stale = { ...op, last_inbound_at: NOW - 86_400_001 };
    expect(unwrap(await perform(fake.fetch, stale))).toEqual({
      status: "delivery_error",
      kind: "permanent",
      sent: "definite_not_sent",
    });
    expect(fake.calls.length).toBe(0);
    unwrap(await perform(fake.fetch, { ...op, last_inbound_at: NOW - 1000 }));
    expect(fake.calls.length).toBe(1);
  });

  test("a malformed op is permanent, not sent, with no request", async () => {
    const fake = fakeFetch(() => Response.json({}));
    const done = unwrap(await perform(fake.fetch, { kind: "text" }));
    expect(done).toMatchObject({
      kind: "permanent",
      sent: "definite_not_sent",
    });
    expect(fake.calls.length).toBe(0);
  });

  test("a refusing fence sends nothing", async () => {
    const fake = fakeFetch(() => Response.json({ messages: [{ id: "x" }] }));
    const lost: SandboxContext = {
      ...CTX,
      fence: async () => err({ code: "stale_epoch", message: "lease lost" }),
    };
    const done = await perform(fake.fetch, op, lost);
    expect(done.ok).toBe(false);
    expect(done.ok ? undefined : done.error.stale?.code).toBe("stale_epoch");
    expect(fake.calls.length).toBe(0);
  });
});

describe("lookup", () => {
  test("is unknown without any request", async () => {
    const fake = fakeFetch(() => Response.json({}));
    const wa = adapter(fake.fetch);
    expect(wa.capabilities.lookup).toBe("none");
    expect(await wa.lookup(KEY, op)).toEqual({
      status: "unknown",
      reason: "whatsapp has no delivery lookup",
    });
    expect(fake.calls.length).toBe(0);
    expect(JSON.stringify(wa)).not.toContain(TOKEN);
  });
});
