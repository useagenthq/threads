import { afterEach, describe, expect, setSystemTime, test } from "bun:test";
import { sqlite } from "@threads/core";
import { storeConnection } from "@threads/core/host";
import { z } from "zod";
import { type Host, host } from "../src";
import { hostTicked } from "../src/host";
import {
  authenticate,
  fakeChannel,
  knownEventsOf,
  mailer,
  say,
  until,
  use,
  webhook,
} from "./kit";
import { sqlAll } from "./sql";

// ask_user over a channel (spec/schema/README.md, "Questions and remembered rules"): the question
// is posted once, the asker's next message answers the oldest open question strictly, a reply
// that matches no option is corrected once, anyone else's message waits, and a question past its
// expiry is closed with "no answer" by the first tick of any host.

const TENANT = "slack:T1";
const alice = { issuer: "slack:T1", tenant: TENANT, subject: "U1" };
const bob = { issuer: "slack:T1", tenant: TENANT, subject: "U2" };
const COLORS = { question: "Which color?", options: ["red", "blue"] };

const hosts: Host[] = [];
afterEach(async () => {
  for (const h of hosts.splice(0)) await h.stop();
  setSystemTime();
});

function message(text: string, key: string, principal = alice): unknown {
  return {
    kind: "message",
    principal,
    address: "C1",
    item_key: key,
    content: text,
  };
}

function hook(delivery: string, items: readonly unknown[]) {
  return webhook({
    tenant: TENANT,
    installation_id: "T1",
    delivery_id: delivery,
    items,
  });
}

function start(responses: readonly unknown[], store = sqlite(":memory:")) {
  const slack = fakeChannel("support", { buttons: false });
  const h = host({
    store,
    authenticate,
    agents: { support: mailer({ responses }) },
    channels: { slack },
  });
  hosts.push(h);
  const post = (delivery: ReturnType<typeof hook>) =>
    h.fetch(
      new Request("http://host.test/channels/slack/events", {
        method: "POST",
        headers: {
          ...delivery.headers,
          "content-type": "application/json",
        },
        body: JSON.stringify(delivery.body),
      }),
    );
  return { h, slack, store, post };
}

async function events(store: ReturnType<typeof sqlite>) {
  const { db } = await storeConnection(store);
  const [row] = z
    .array(z.strictObject({ branch_id: z.string() }))
    .parse(await sqlAll(db, "SELECT branch_id FROM branches", []));
  return row === undefined ? [] : knownEventsOf(store, TENANT, row.branch_id);
}

const texts = (performed: readonly { readonly op: unknown }[]) =>
  performed.map((p) => z.object({ text: z.string() }).parse(p.op).text);

async function asked(responses: readonly unknown[]) {
  const s = start([use("ask_user", COLORS, "q1"), ...responses]);
  await s.h.ready();
  await s.post(hook("E1", [message("Paint it.", "E1#0")]));
  await until(async () => s.slack.performed.length === 1);
  return s;
}

describe("ask_user over a channel", () => {
  test("the question is posted once with numbered options; the asker's 2 records option 2", async () => {
    const s = await asked([say("Blue it is.")]);
    expect(texts(s.slack.performed)).toEqual([
      "Which color?\n\n1. red\n2. blue\n\nReply with the number or the text of your choice.",
    ]);
    await s.post(hook("E2", [message("2", "E2#0")]));
    await until(async () => s.slack.performed.length === 2);
    expect(texts(s.slack.performed)[1]).toBe("Blue it is.");
    const answered = (await events(s.store)).find(
      (e) => e.type === "tool_result" && e.data.call_id === "q1",
    );
    expect(answered).toMatchObject({
      actor: { kind: "user", principal: alice },
      data: { call_id: "q1", origin: "answered", preview: "blue" },
    });
  });

  test("another member's message waits as ordinary input; the asker still answers", async () => {
    const s = await asked([say("Red then."), say("Hi Bob.")]);
    await s.post(hook("E2", [message("hello", "E2#0", bob)]));
    await s.post(hook("E3", [message("red", "E3#0")]));
    await until(async () => s.slack.performed.length === 3);
    expect(texts(s.slack.performed).slice(1)).toEqual(["Red then.", "Hi Bob."]);
    const inputs = (await events(s.store)).filter(
      (e) => e.type === "user_input",
    );
    expect(inputs.map((e) => e.actor)).toEqual([
      { kind: "user", principal: alice },
      { kind: "user", principal: bob },
    ]);
  });

  test("a reply that is no option is rejected once, corrected once, and the question stays open", async () => {
    const s = await asked([say("Red then.")]);
    const wrong = hook("E2", [message("green", "E2#0")]);
    await s.post(wrong);
    await until(async () => s.slack.performed.length === 2);
    // The provider redelivers after a lost ack: the inbox keeps one item, so one correction.
    await s.post(wrong);
    for (let i = 0; i < 2; i++) await hostTicked(s.h);
    expect(s.slack.performed).toHaveLength(2);
    expect(texts(s.slack.performed)[1]).toBe(
      "Please answer with one of:\n\n1. red\n2. blue\n\nReply with the number or the text of your choice.",
    );
    const log = await events(s.store);
    expect(log.filter((e) => e.type === "answer_rejected")).toHaveLength(1);
    expect(log.at(-1)?.type).toBe("tool_result");
    await s.post(hook("E3", [message("RED", "E3#0")]));
    await until(async () => s.slack.performed.length === 3);
    expect(texts(s.slack.performed)).toHaveLength(3);
  });

  test("each consumed item's seq is its channel_delivery's: a new thread's message, a rejection, an answer", async () => {
    const s = await asked([say("Red then.")]);
    await s.post(hook("E2", [message("green", "E2#0")]));
    await until(async () => s.slack.performed.length === 2);
    await s.post(hook("E3", [message("red", "E3#0")]));
    await until(async () => s.slack.performed.length === 3);
    const { db } = await storeConnection(s.store);
    const consumed = z
      .array(z.object({ item_key: z.string(), consumed_seq: z.number() }))
      .parse(
        await sqlAll(
          db,
          "SELECT item_key, consumed_seq FROM inbox ORDER BY inbox_id",
        ),
      );
    const delivered = (await events(s.store)).flatMap((e) =>
      e.type === "channel_delivery"
        ? [{ item_key: e.data.item_key, consumed_seq: e.seq }]
        : [],
    );
    expect(consumed).toEqual(delivered);
    expect(consumed.map((c) => c.item_key)).toEqual(["E1#0", "E2#0", "E3#0"]);
  });

  test("two questions in one response are asked in turn, each answered by the next reply", async () => {
    const two = {
      content: [
        { type: "tool_use", call_id: "q1", name: "ask_user", input: COLORS },
        {
          type: "tool_use",
          call_id: "q2",
          name: "ask_user",
          input: { question: "Why?" },
        },
      ],
      stop_reason: "tool_use",
      usage: { input_tokens: 1, output_tokens: 1 },
    };
    const s = start([two, say("Done.")]);
    await s.h.ready();
    await s.post(hook("E1", [message("Paint it.", "E1#0")]));
    await until(async () => s.slack.performed.length === 1);
    await s.post(hook("E2", [message("blue", "E2#0")]));
    await s.post(hook("E3", [message("It matches.", "E3#0")]));
    await until(async () => s.slack.performed.length === 3);
    expect(texts(s.slack.performed).slice(1)).toEqual(["Why?", "Done."]);
    const answers = (await events(s.store)).flatMap((e) =>
      e.type === "tool_result" && e.data.origin === "answered"
        ? [[e.data.call_id, e.data.preview]]
        : [],
    );
    expect(answers).toEqual([
      ["q1", "blue"],
      ["q2", "It matches."],
    ]);
  });

  test("a question that expired while no host ran is closed by the next host's first tick", async () => {
    const store = sqlite(":memory:");
    const first = start([use("ask_user", COLORS, "q1")], store);
    await first.h.ready();
    await first.post(hook("E1", [message("Paint it.", "E1#0")]));
    await until(async () => first.slack.performed.length === 1);
    await first.h.stop();
    setSystemTime(new Date(Date.now() + 25 * 3_600_000));
    const next = start([say("I went with red.")], store);
    await next.h.ready();
    await until(async () =>
      (await events(store)).some((e) => e.type === "turn_completed"),
    );
    const log = await events(store);
    expect<unknown>(
      log.flatMap((e) =>
        e.type === "tool_result" && e.data.call_id === "q1" ? [e.data] : [],
      ),
    ).toEqual([
      {
        call_id: "q1",
        is_error: true,
        origin: "not_executed",
        completeness: "complete",
        preview: "no answer",
      },
    ]);
    const { db } = await storeConnection(store);
    expect(await sqlAll(db, "SELECT state FROM questions", [])).toEqual([
      { state: "expired" },
    ]);
  });
});
