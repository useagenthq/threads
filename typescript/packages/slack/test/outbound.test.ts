import { describe, expect, test } from "bun:test";
import {
  type ChannelAdapter,
  FenceRefused,
  type Fetch,
  within,
} from "@threads/core/adapter";
import { KnownEvent } from "../../core/src/log";
import { err } from "../../core/src/result";
import type { SandboxContext } from "../../core/src/sandbox";
import { CTX } from "../../core/test/sandbox/context";
import { ROOT, T0, THREAD } from "../../core/test/store/helpers";
import { adapter, CHALLENGE, TOKEN } from "./kit";

// Outbound over a fake transport (no network): one fenced chat.postMessage per perform with the
// effect key in metadata, outcomes mapped to what is provable, and lookup by that metadata.

type Op = Parameters<ChannelAdapter["perform"]>[0];
type Sent = { readonly url: string; readonly body: URLSearchParams };

function transport(reply: () => Response | Promise<Response>) {
  const sent: Sent[] = [];
  const fetch: Fetch = async (input, init) => {
    sent.push({
      url: String(input),
      body: new URLSearchParams(String(init?.body)),
    });
    return reply();
  };
  return { sent, fetch };
}

const json = (body: object, status = 200): Response =>
  Response.json(body, { status });
const message = {
  kind: "message",
  address: "C1:1.1",
  installation_id: "T1",
  text: "hi",
};

function event(type: string, data: object): KnownEvent {
  return KnownEvent.parse({
    seq: 1,
    event_id: "0192b000-0000-7000-8000-0000000000e1",
    thread_id: THREAD,
    branch_id: ROOT,
    epoch: 1,
    time: T0,
    prev_hash: "a".repeat(64),
    type,
    type_version: 1,
    critical: true,
    actor: type === "model_response" ? { kind: "model" } : { kind: "host" },
    data,
  });
}

describe("render", () => {
  test("a model response is one message op of its joined text", () => {
    const e = event("model_response", {
      request_event_id: "0192b000-0000-7000-8000-0000000000e0",
      content: [
        { type: "text", text: "a" },
        { type: "tool_use", call_id: "call_1", name: "t", input: {} },
        { type: "text", text: "b" },
      ],
      stop_reason: "end_turn",
      usage: { input_tokens: 1, output_tokens: 1 },
      completeness: "complete",
    });
    expect(adapter().render(e)).toEqual([{ kind: "message", text: "ab" }]);
  });

  test("an approval request is an approval op bound to its challenge id", () => {
    const e = event("approval_requested", {
      challenge_id: CHALLENGE,
      call_id: "call_1",
      args_hash: "b".repeat(64),
      expires_at: T0 + 1,
    });
    expect(adapter().render(e)).toEqual([
      {
        kind: "approval",
        challenge_id: CHALLENGE,
        text: "Approval needed for call call_1.",
      },
    ]);
  });
});

async function perform(
  fetch: Fetch,
  op: Op = message,
  context: SandboxContext = CTX,
) {
  const done = await within(context, () =>
    adapter({ fetch }).perform(op, "branch:call_1", { botToken: TOKEN }),
  );
  if (!done.ok) throw done.error.error;
  return done.value;
}

describe("perform", () => {
  test("posts once with the effect key in metadata and answers the platform ref", async () => {
    const t = transport(() => json({ ok: true, channel: "C1", ts: "9.9" }));
    expect(await perform(t.fetch, { ...message, last_inbound_at: T0 })).toEqual(
      {
        status: "sent",
        platform_ref: "C1:9.9",
      },
    );
    expect(t.sent).toHaveLength(1);
    const body = t.sent[0]?.body;
    expect(t.sent[0]?.url).toEndWith("/chat.postMessage");
    expect(body?.get("channel")).toBe("C1");
    expect(body?.get("thread_ts")).toBe("1.1");
    expect(JSON.parse(body?.get("metadata") ?? "")).toEqual({
      event_type: "threads_effect",
      event_payload: { effect_key: "branch:call_1" },
    });
  });

  test("an approval posts buttons whose values carry only the challenge and decision", async () => {
    const t = transport(() => json({ ok: true, channel: "C1", ts: "9.9" }));
    await perform(t.fetch, {
      kind: "approval",
      address: "C1",
      challenge_id: CHALLENGE,
      text: "ok?",
    });
    const blocks = JSON.parse(t.sent[0]?.body.get("blocks") ?? "");
    const values = blocks[1].elements.map((b: { value: string }) =>
      JSON.parse(b.value),
    );
    expect(values).toEqual([
      { challenge_id: CHALLENGE, decision: "grant" },
      { challenge_id: CHALLENGE, decision: "deny" },
    ]);
  });

  test("outcomes: only what provably never reached Slack is definite_not_sent", async () => {
    const refused = Object.assign(new Error("refused"), {
      code: "ConnectionRefused",
    });
    const cases: [() => Response, object][] = [
      [
        () => json({ ok: false, error: "channel_not_found" }),
        ["permanent", "definite_not_sent"],
      ],
      [
        () => json({ ok: false, error: "internal_error" }),
        ["transient", "outcome_unknown"],
      ],
      [() => json({}, 429), ["rate_limited", "definite_not_sent"]],
      [
        () =>
          new Response("x", { status: 429, headers: { "retry-after": "1" } }),
        ["rate_limited", "definite_not_sent"],
      ],
      [
        () => new Response("bad gateway", { status: 502 }),
        ["transient", "outcome_unknown"],
      ],
      [
        () => {
          throw refused;
        },
        ["transient", "definite_not_sent"],
      ],
      [
        () => {
          throw new Error("socket reset");
        },
        ["transient", "outcome_unknown"],
      ],
      [() => json({ ok: true }), ["transient", "outcome_unknown"]],
    ];
    for (const [reply, [kind, sent]] of cases.map(
      ([r, o]) => [r, Object.values(o)] as const,
    )) {
      const t = transport(reply);
      const out = await perform(t.fetch);
      expect(out).toEqual({ status: "delivery_error", kind, sent });
      expect(t.sent).toHaveLength(1);
      expect(JSON.stringify(out)).not.toContain(TOKEN);
    }
  });

  test("an invalid op or a missing token sends nothing", async () => {
    const t = transport(() => json({ ok: true }));
    expect(
      await perform(t.fetch, { kind: "message", text: "no address" }),
    ).toMatchObject({
      kind: "permanent",
    });
    expect(
      await adapter({ fetch: t.fetch }).perform(message, "k", {}),
    ).toMatchObject({
      sent: "definite_not_sent",
    });
    expect(t.sent).toHaveLength(0);
  });

  test("outside within() or behind a refusing fence nothing is sent", async () => {
    const t = transport(() => json({ ok: true, channel: "C1", ts: "9.9" }));
    const slack = adapter({ fetch: t.fetch });
    expect(
      slack.perform(message, "k", { botToken: TOKEN }),
    ).rejects.toBeInstanceOf(FenceRefused);
    const stale: SandboxContext = {
      ...CTX,
      fence: async () => err({ code: "stale_epoch", message: "lease lost" }),
    };
    const done = await within(stale, () =>
      slack.perform(message, "k", { botToken: TOKEN }),
    );
    expect(done.ok ? "sent" : done.error.stale?.code).toBe("stale_epoch");
    expect(t.sent).toHaveLength(0);
  });
});

describe("lookup", () => {
  const tagged = (key: string) => ({
    ts: "9.9",
    metadata: {
      event_type: "threads_effect",
      event_payload: { effect_key: key },
    },
  });

  async function lookup(fetch: Fetch, op: Op = message) {
    const done = await within(CTX, () =>
      adapter({ fetch }).lookup("branch:call_1", op),
    );
    if (!done.ok) throw done.error.error;
    return done.value;
  }

  test("finds the post by its metadata effect key, in the thread", async () => {
    const t = transport(() =>
      json({ ok: true, messages: [{ ts: "1.1" }, tagged("branch:call_1")] }),
    );
    expect(await lookup(t.fetch)).toEqual({ status: "found", value: "C1:9.9" });
    expect(t.sent[0]?.url).toEndWith("/conversations.replies");
    expect(t.sent[0]?.body.get("include_all_metadata")).toBe("true");
  });

  test("absence in the scanned window is only nonfinal; errors are unknown", async () => {
    const miss = transport(() =>
      json({ ok: true, messages: [tagged("other")] }),
    );
    expect(await lookup(miss.fetch, { ...message, address: "C1" })).toEqual({
      status: "not_found_nonfinal",
    });
    expect(miss.sent[0]?.url).toEndWith("/conversations.history");
    const bad = transport(() => json({ ok: false, error: "invalid_auth" }));
    const out = await lookup(bad.fetch);
    expect(out).toEqual({
      status: "unknown",
      reason: "slack lookup: invalid_auth",
    });
    expect(JSON.stringify(out)).not.toContain(TOKEN);
    expect(adapter().capabilities.lookup).toBe("nonfinal");
  });
});
