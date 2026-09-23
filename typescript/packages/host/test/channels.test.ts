import { afterEach, describe, expect, test } from "bun:test";
import { credential } from "@threads/core/adapter";
import { openStore, storeConnection } from "@threads/core/host";
import { z } from "zod";
import {
  eventsOf,
  type FakeChannel,
  fakeChannel,
  type Harness,
  harness,
  mailer,
  say,
  until,
  use,
  webhook,
} from "./kit";

// Channel intake to reply: the inbox before the response, one run per item, replies
// as channel_send effects with delivery certainty, approvals only from configured approvers.

const TENANT = "slack:T1";
const alice = { issuer: "slack:T1", tenant: TENANT, subject: "U1" };
const mallory = { issuer: "slack:T1", tenant: TENANT, subject: "U666" };

let h: Harness | undefined;
afterEach(async () => {
  await h?.host.stop();
  h = undefined;
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

async function post(
  harnessed: Harness,
  delivery: ReturnType<typeof hook>,
): Promise<Response> {
  return harnessed.call("POST", "/channels/slack/events", delivery);
}

function setup(
  responses: readonly unknown[],
  options: {
    readonly approvers?: readonly (typeof alice)[];
    readonly channel?: Partial<FakeChannel["capabilities"]>;
  } = {},
): { readonly h: Harness; readonly slack: FakeChannel } {
  const slack = fakeChannel("support", options.channel);
  h = harness({
    agents: {
      support: mailer({
        responses,
        ...(options.approvers === undefined
          ? {}
          : { approvers: options.approvers }),
      }),
    },
    channels: { slack },
  });
  return { h, slack };
}

async function inbox(harnessed: Harness) {
  const { db } = await storeConnection(harnessed.store);
  return z
    .array(
      z.strictObject({
        item_key: z.string(),
        thread_id: z.string(),
        consumed_seq: z.int().nullable(),
      }),
    )
    .parse(
      db.all(
        "SELECT item_key, thread_id, consumed_seq FROM inbox ORDER BY inbox_id",
        [],
      ),
    );
}

async function branchOf(harnessed: Harness, threadId: string): Promise<string> {
  const { db } = await storeConnection(harnessed.store);
  const rows = z
    .array(z.strictObject({ branch_id: z.string() }))
    .parse(
      db.all(
        "SELECT branch_id FROM branches WHERE thread_id = ? AND parent_branch_id IS NULL",
        [threadId],
      ),
    );
  return rows[0]?.branch_id ?? "";
}

async function threadEvents(harnessed: Harness) {
  const [row] = await inbox(harnessed);
  if (row === undefined) return [];
  const branch = await branchOf(harnessed, row.thread_id);
  return branch === "" ? [] : eventsOf(harnessed.store, TENANT, branch);
}

describe("intake", () => {
  test("a forged webhook is 401 and stores nothing", async () => {
    const { h } = setup([say("Hi")]);
    const response = await post(h, {
      ...hook("E1", [message("hi", "E1#0")]),
      headers: { "x-signature": "bad" },
    });
    expect(response.status).toBe(401);
    expect(await inbox(h)).toEqual([]);
  });

  test("the item is in the inbox before the ack; ready() starts no run and sends nothing", async () => {
    const { h, slack } = setup([say("Hello back")]);
    const response = await post(h, hook("E1", [message("hi", "E1#0")]));
    expect(response.status).toBe(200);
    expect(await response.text()).toBe("ok");
    expect(await inbox(h)).toMatchObject([
      { item_key: "E1#0", consumed_seq: null },
    ]);
    await h.host.ready();
    expect(slack.performed).toEqual([]);
    expect(await threadEvents(h)).toEqual([]);
  });
});

describe("a message runs once and its reply is an effect", () => {
  test("channel_delivery, user_input, then channel_send committed with the platform ref", async () => {
    const { h, slack } = setup([say("Hello back")]);
    await h.host.ready();
    await post(h, hook("E1", [message("hi", "E1#0")]));
    await post(h, hook("E1", [message("hi", "E1#0")]));
    await until(async () =>
      (await threadEvents(h)).some((e) => e.type === "effect_commit"),
    );
    const events = await threadEvents(h);
    const types = events.map((e) => e.type);
    expect(types.filter((t) => t === "user_input")).toHaveLength(1);
    expect(types.slice(0, 3)).toEqual([
      "thread_started",
      "channel_delivery",
      "user_input",
    ]);
    const call = events.find((e) => e.type === "tool_call");
    expect(call?.["data"]).toMatchObject({
      name: "channel_send",
      input: {
        kind: "text",
        text: "Hello back",
        address: "C1",
        installation_id: "T1",
      },
    });
    expect(
      z.object({ call_id: z.string() }).parse(call?.["data"]).call_id,
    ).toMatch(/^send_\d+_0$/);
    expect(slack.performed).toHaveLength(1);
    expect((await inbox(h))[0]?.consumed_seq).toBeGreaterThan(0);
  });

  test("an unknown outcome is looked up, never re-sent: found settles it", async () => {
    const { h, slack } = setup([say("Hello back")]);
    slack.outcomes.push({
      status: "delivery_error",
      kind: "transient",
      sent: "outcome_unknown",
    });
    slack.lookups.push("found");
    await h.host.ready();
    await post(h, hook("E1", [message("hi", "E1#0")]));
    await until(async () =>
      (await threadEvents(h)).some((e) => e.type === "tool_result"),
    );
    const types = (await threadEvents(h)).map((e) => e.type);
    expect(types).toContain("effect_unknown");
    expect(slack.performed).toHaveLength(1);
    const resolved = (await threadEvents(h)).find(
      (e) => e.type === "effect_resolved",
    );
    expect(resolved?.["data"]).toMatchObject({
      outcome: "confirmed_success",
      by: "reconcile",
    });
  });

  test("an unknown outcome with no final answer parks", async () => {
    const { h, slack } = setup([say("Hello back")], {
      channel: { lookup: "nonfinal" },
    });
    slack.outcomes.push({
      status: "delivery_error",
      kind: "transient",
      sent: "outcome_unknown",
    });
    slack.lookups.push("not_found");
    await h.host.ready();
    await post(h, hook("E1", [message("hi", "E1#0")]));
    await until(async () =>
      (await threadEvents(h)).some((e) => e.type === "parked"),
    );
    expect(slack.performed).toHaveLength(1);
    const parked = (await threadEvents(h)).findLast((e) => e.type === "parked");
    expect(parked?.["data"]).toMatchObject({ reason: "effect_unknown" });
  });

  test("a receipt holding a registered value is stored redacted (#328 HIGH 9)", async () => {
    const key = credential("fake", "apiKey", "sk-l9-receipt-5c6d", "U")();
    const { h, slack } = setup([say("Hello back")]);
    slack.outcomes.push({ status: "sent", platform_ref: `msg ${key}` });
    await h.host.ready();
    await post(h, hook("E1", [message("hi", "E1#0")]));
    await until(async () =>
      (await threadEvents(h)).some((e) => e.type === "effect_commit"),
    );
    const commit = (await threadEvents(h)).find(
      (e) => e.type === "effect_commit",
    );
    const { result_ref } = z
      .object({ result_ref: z.object({ sha256: z.string() }) })
      .parse(commit?.["data"]);
    const { artifacts } = await openStore(h.store);
    const stored = artifacts.get(result_ref.sha256);
    if (!stored.ok) throw new Error(stored.error.message);
    expect(new TextDecoder().decode(stored.value)).toBe(
      "msg [secret fake.apiKey]",
    );
  });

  test("definite_not_sent transient is re-sent under the same key", async () => {
    const { h, slack } = setup([say("Hello back")]);
    slack.outcomes.push({
      status: "delivery_error",
      kind: "transient",
      sent: "definite_not_sent",
    });
    await h.host.ready();
    await post(h, hook("E1", [message("hi", "E1#0")]));
    await until(async () =>
      (await threadEvents(h)).some((e) => e.type === "effect_commit"),
    );
    expect(slack.performed).toHaveLength(2);
    expect(slack.performed[0]?.key).toBe(slack.performed[1]?.key ?? "");
  });
});

describe("approvals over a channel", () => {
  const ask = [use("send_email", { to: "bob" }, "c1"), say("Sent.")];

  async function parked(approvers?: readonly (typeof alice)[]) {
    const { h, slack } = setup(
      ask,
      approvers === undefined ? {} : { approvers },
    );
    await h.host.ready();
    await post(h, hook("E1", [message("mail bob", "E1#0")]));
    await until(async () =>
      slack.performed.some((p) => JSON.stringify(p.op).includes("approval")),
    );
    const card = slack.performed.find((p) =>
      JSON.stringify(p.op).includes("approval"),
    );
    const challenge = z
      .object({ challenge_id: z.string() })
      .parse(card?.op).challenge_id;
    return { h, slack, challenge };
  }

  function decision(
    challenge: string,
    key: string,
    principal = alice,
  ): unknown {
    return {
      kind: "decision",
      principal,
      address: "C1",
      item_key: key,
      challenge_id: challenge,
      decision: "grant",
    };
  }

  test("an approver's button resumes the call once; the duplicate delivery settles nothing", async () => {
    const { h, challenge } = await parked([alice]);
    await post(h, hook("E2", [decision(challenge, "E2#0")]));
    await post(h, hook("E3", [decision(challenge, "E3#0")]));
    await until(async () =>
      (await threadEvents(h)).some((e) => e.type === "turn_completed"),
    );
    const types = (await threadEvents(h)).map((e) => e.type);
    expect(types.filter((t) => t === "approval_granted")).toHaveLength(1);
  });

  test("without configured approvers only the run's originating sender approves", async () => {
    const { h, challenge } = await parked();
    await post(h, hook("E2", [decision(challenge, "E2#0", mallory)]));
    await until(async () =>
      (await inbox(h)).every((r) => r.consumed_seq !== null),
    );
    expect((await threadEvents(h)).map((e) => e.type)).not.toContain(
      "approval_granted",
    );
    await post(h, hook("E3", [decision(challenge, "E3#0")]));
    await until(async () =>
      (await threadEvents(h)).some((e) => e.type === "approval_granted"),
    );
  });

  test("a sender who is not an approver approves nothing", async () => {
    const { h, challenge } = await parked([alice]);
    await post(h, hook("E2", [decision(challenge, "E2#0", mallory)]));
    await until(async () =>
      (await inbox(h)).every((r) => r.consumed_seq !== null),
    );
    expect((await threadEvents(h)).map((e) => e.type)).not.toContain(
      "approval_granted",
    );
  });

  test("a free-text yes is a message, not an approval", async () => {
    const { h } = await parked([alice]);
    await post(h, hook("E2", [message("yes", "E2#0")]));
    await Bun.sleep(100);
    expect((await threadEvents(h)).map((e) => e.type)).not.toContain(
      "approval_granted",
    );
  });
});
