import { afterEach, describe, expect, test } from "bun:test";
import { agent } from "@threads/core";
import { z } from "zod";
import { uiThreadId } from "../../src/ui/key";
import { alice, type Harness, harness, mailer, say, use } from "../kit";
import { chat, settled } from "./clients";
import { aiSdkUninterrupted, Gate, inputs, liveModel } from "./e2e-kit";
import { aiSdkProjection } from "./stock";

// The stock AI SDK 7 client (Chat through DefaultChatTransport) against the host: a message, an
// approval answered with addToolApprovalResponse and the combined sendAutomaticallyWhen, a
// dropped stream resumed with resumeStream, and 204 once the run has ended. After each, the
// Chat's assistant message equals an uninterrupted client's: the committed frames of the run
// from its start, folded by the SDK's own processUIMessageStream.

let h: Harness | undefined;
afterEach(async () => {
  await h?.host.stop();
  h = undefined;
});

const Assistant = z.object({ id: z.string(), role: z.literal("assistant") });

async function uninterrupted(run: string): Promise<unknown[]> {
  if (h === undefined) throw new Error("no host");
  const thread = uiThreadId(alice, "support", "chat-1");
  const message = await aiSdkUninterrupted(h.host, alice, thread, run);
  return aiSdkProjection(
    z.custom<Parameters<typeof aiSdkProjection>[0]>().parse(message),
  );
}

function last(c: { readonly messages: readonly unknown[] }): unknown[] {
  return aiSdkProjection(
    z.custom<Parameters<typeof aiSdkProjection>[0]>().parse(c.messages.at(-1)),
  );
}

describe("AI SDK end to end", () => {
  test("a message streams its run; one user_input carries the client's message id", async () => {
    h = harness({
      agents: {
        support: agent({
          name: "support",
          model: liveModel([say("Hi there, Alice.")]),
        }),
      },
    });
    const { chat: c } = chat(h.host, alice, "chat-1");
    await c.sendMessage({ text: "Hello" });
    await settled(c);
    expect(c.status).toBe("ready");
    const run = Assistant.parse(c.messages.at(-1)).id;
    expect(last(c)).toEqual(await uninterrupted(run));
    const recorded = await inputs(
      h.store,
      "acme",
      uiThreadId(alice, "support", "chat-1"),
    );
    expect(recorded.map((e) => e.data.client_message_id)).toEqual([
      c.messages[0]?.id,
    ]);
    expect(recorded.map((e) => String(e.event_id))).toEqual([run]);
  });

  test("an approval answered with addToolApprovalResponse sends automatically and resumes", async () => {
    const sent: string[] = [];
    h = harness({
      agents: {
        support: mailer({
          responses: [use("send_email", { to: "bob" }, "m1"), say("Mailed.")],
          sent,
        }),
      },
    });
    const { chat: c, log } = chat(h.host, alice, "chat-1");
    await c.sendMessage({ text: "Mail bob" });
    await settled(c);
    const part = z
      .object({ approval: z.object({ id: z.string() }) })
      .parse(c.messages.at(-1)?.parts.find((p) => "approval" in p));
    await c.addToolApprovalResponse({ id: part.approval.id, approved: true });
    await settled(c, () => JSON.stringify(c.messages).includes("Mailed."));
    expect(sent).toEqual(["bob"]);
    expect(log.requests.filter((r) => r.method === "POST")).toHaveLength(2);
    const run = Assistant.parse(c.messages.at(-1)).id;
    expect(last(c)).toEqual(await uninterrupted(run));
    expect(c.messages).toHaveLength(2);
  });

  test("ask_user answered with addToolOutput sends automatically and resumes", async () => {
    h = harness({
      agents: {
        support: agent({
          name: "support",
          model: liveModel([
            use(
              "ask_user",
              { question: "Which colour?", options: ["Red", "Blue"] },
              "q1",
            ),
            say("Blue it is."),
          ]),
        }),
      },
    });
    const { chat: c, log } = chat(h.host, alice, "chat-1");
    await c.sendMessage({ text: "Paint it" });
    await settled(c);
    const asked = z
      .object({ state: z.literal("input-available"), toolCallId: z.string() })
      .parse(c.messages.at(-1)?.parts.find((p) => p.type === "tool-ask_user"));
    await c.addToolOutput({
      tool: "ask_user",
      toolCallId: asked.toolCallId,
      output: "Blue",
    });
    await settled(c, () => JSON.stringify(c.messages).includes("Blue it is."));
    expect(c.error).toBeUndefined();
    expect(log.requests.filter((r) => r.method === "POST")).toHaveLength(2);
    const run = Assistant.parse(c.messages.at(-1)).id;
    expect(last(c)).toEqual(await uninterrupted(run));
    expect(c.messages).toHaveLength(2);
  });

  test("a second tab approving an already approved call streams the run, with no error", async () => {
    const sent: string[] = [];
    h = harness({
      agents: {
        support: mailer({
          responses: [use("send_email", { to: "bob" }, "m1"), say("Mailed.")],
          sent,
        }),
      },
    });
    const { chat: a } = chat(h.host, alice, "chat-1");
    await a.sendMessage({ text: "Mail bob" });
    await settled(a);
    const { chat: b, log } = chat(
      h.host,
      alice,
      "chat-1",
      "support",
      structuredClone(a.messages),
    );
    const part = z
      .object({ approval: z.object({ id: z.string() }) })
      .parse(a.messages.at(-1)?.parts.find((p) => "approval" in p));
    await a.addToolApprovalResponse({ id: part.approval.id, approved: true });
    await settled(a, () => JSON.stringify(a.messages).includes("Mailed."));
    await b.addToolApprovalResponse({ id: part.approval.id, approved: true });
    await settled(b, () => log.requests.some((r) => r.method === "POST"));
    expect(b.error?.message).toBeUndefined();
    expect(b.status).toBe("ready");
    expect(sent).toEqual(["bob"]);
    const run = Assistant.parse(b.messages.at(-1)).id;
    expect(last(b)).toEqual(await uninterrupted(run));
    expect(last(b)).toEqual(last(a));
  });

  test("a stream dropped during live text resumes with resumeStream: one assistant message", async () => {
    const gate = new Gate();
    h = harness({
      agents: {
        support: agent({
          name: "support",
          model: liveModel([say("The answer is forty two.")], { at: 0, gate }),
        }),
      },
    });
    const { chat: c, log } = chat(h.host, alice, "chat-1");
    log.dropAfter = 4;
    await c.sendMessage({ text: "What is the answer?" });
    expect(c.status).toBe("error");
    const resumed = c.resumeStream();
    while (!log.requests.some((r) => r.method === "GET")) await Bun.sleep(5);
    await Bun.sleep(50);
    gate.open();
    await resumed;
    await settled(c);
    expect(c.messages).toHaveLength(2);
    const run = Assistant.parse(c.messages.at(-1)).id;
    expect(last(c)).toEqual(await uninterrupted(run));
    const recorded = await inputs(
      h.store,
      "acme",
      uiThreadId(alice, "support", "chat-1"),
    );
    expect(recorded).toHaveLength(1);
  });

  test("resumeStream once the run has ended is 204 and changes nothing", async () => {
    h = harness({
      agents: {
        support: agent({ name: "support", model: liveModel([say("Done.")]) }),
      },
    });
    const { chat: c, log } = chat(h.host, alice, "chat-1");
    await c.sendMessage({ text: "Go" });
    await settled(c);
    const before = structuredClone(c.messages);
    await c.resumeStream();
    expect(c.status).toBe("ready");
    expect(c.messages).toEqual(before);
    const reconnect = log.requests.find((r) => r.method === "GET");
    expect(reconnect?.url).toBe(
      "http://host.test/v1/ui/ai-sdk/support/chat-1/stream",
    );
  });
});
