import { afterEach, describe, expect, test } from "bun:test";
import { sqlite } from "@threads/core";
import { openStore, storeConnection, tenantStore } from "@threads/core/host";
import { z } from "zod";
import { type Host, host } from "../src";
import { hostTicked } from "../src/host";
import {
  authenticate,
  eventsOf,
  type FakeChannel,
  fakeChannel,
  harness,
  mailer,
  say,
  until,
  use,
  webhook,
} from "./kit";

// Channel replies are derived from the log (spec/schema/README.md, "Channel replies"): a crash
// between a turn's end and its reply's channel_send loses nothing and sends nothing twice, and
// an approval card or reply goes to the conversation whatever ran the thread.

const TENANT = "slack:T1";
const alice = { issuer: "slack:T1", tenant: TENANT, subject: "U1" };

const hosts: Host[] = [];
afterEach(async () => {
  for (const h of hosts.splice(0)) await h.stop();
});

function message(text: string, key: string): unknown {
  return {
    kind: "message",
    principal: alice,
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

async function post(h: Host, delivery: ReturnType<typeof hook>) {
  return h.fetch(
    new Request("http://host.test/channels/slack/events", {
      method: "POST",
      headers: { ...delivery.headers, "content-type": "application/json" },
      body: JSON.stringify(delivery.body),
    }),
  );
}

async function threadEvents(store: ReturnType<typeof sqlite>) {
  const { db } = await storeConnection(store);
  const [row] = z
    .array(z.object({ branch_id: z.string() }))
    .parse(db.all("SELECT branch_id FROM branches", []));
  return row === undefined ? [] : eventsOf(store, TENANT, row.branch_id);
}

const sends = (events: readonly { readonly type: string }[]) =>
  events.filter(
    (e) =>
      e.type === "tool_call" &&
      z.object({ data: z.object({ name: z.string() }) }).parse(e).data.name ===
        "channel_send",
  );

describe("a crash between turn_completed and the reply", () => {
  test("recovery after ready() issues the missing channel_send once, never twice", async () => {
    const store = sqlite(":memory:");
    const dying: FakeChannel = {
      ...fakeChannel("support"),
      render: () => {
        throw new Error("the process died before the reply was issued");
      },
    };
    const first = host({
      store,
      authenticate,
      agents: { support: mailer({ responses: [say("Hello back")] }) },
      channels: { slack: dying },
    });
    hosts.push(first);
    await first.ready();
    await post(first, hook("E1", [message("hi", "E1#0")]));
    await until(async () =>
      (await threadEvents(store)).some((e) => e.type === "turn_completed"),
    );
    await first.stop();
    expect(sends(await threadEvents(store))).toHaveLength(0);

    // `hold` runs before each send, so a test can keep a tick inside recovery.
    const restart = (
      hold: () => Promise<void> = async () => {},
    ): { readonly h: Host; readonly slack: FakeChannel } => {
      const base = fakeChannel("support");
      const slack: FakeChannel = {
        ...base,
        perform: async (op, key, credentials) => {
          await hold();
          return base.perform(op, key, credentials);
        },
      };
      const h = host({
        store,
        authenticate,
        agents: { support: mailer({ responses: [] }) },
        channels: { slack },
      });
      hosts.push(h);
      return { h, slack };
    };
    const gate = Promise.withResolvers<void>();
    const sending = Promise.withResolvers<void>();
    const second = restart(async () => {
      sending.resolve();
      await gate.promise;
    });
    const firstTick = hostTicked(second.h);
    await second.h.ready();
    await sending.promise;
    // Asked while a tick runs: it waits for the next tick, not the one in flight.
    const nextTick = hostTicked(second.h);
    gate.resolve();
    await firstTick;
    // The tick hostTicked observed includes reply recovery.
    expect(second.slack.performed).toHaveLength(1);
    expect(second.slack.performed[0]?.op).toMatchObject({
      kind: "text",
      text: "Hello back",
      address: "C1",
    });
    expect(await Promise.race([nextTick, Promise.resolve("pending")])).toBe(
      "pending",
    );
    await nextTick;
    await second.h.stop();

    const third = restart();
    await third.h.ready();
    await hostTicked(third.h);
    expect(third.slack.performed).toHaveLength(0);
    expect(sends(await threadEvents(store))).toHaveLength(1);
  }, 15_000);
});

describe("a run lost after its input is durable", () => {
  test("the host that consumed the message runs it on a later tick and replies", async () => {
    const slack = fakeChannel("support");
    const store = sqlite(":memory:");
    const h = host({
      store,
      authenticate,
      agents: { support: mailer({ responses: [say("Hello back")] }) },
      channels: { slack },
    });
    hosts.push(h);
    await h.ready();
    // Past the first tick, which looks only at the conversations that existed at startup.
    await hostTicked(h);
    // Another host's short lease takes the branch between the user_input's append and the run's
    // own lease, once: the turn stays open, and no run is going on it anywhere.
    const { log } = await openStore(tenantStore(store, TENANT));
    const acquire = log.acquire.bind(log);
    let lost = false;
    log.acquire = (branch, holder, ttl) => {
      if (lost || !holder.startsWith("run-"))
        return acquire(branch, holder, ttl);
      lost = true;
      return {
        ok: false,
        error: { code: "branch_busy", message: "another host's lease" },
      };
    };
    await post(h, hook("E1", [message("hi", "E1#0")]));
    await until(async () => lost);
    await until(async () => slack.performed.length === 1, 5_000);
    expect(slack.performed[0]?.op).toMatchObject({ text: "Hello back" });
  }, 10_000);
});

describe("approvals go to the originating channel", () => {
  test("a channel thread approved over the API still replies in its conversation", async () => {
    const slack = fakeChannel("support");
    const h = harness({
      agents: {
        support: mailer({
          responses: [use("send_email", { to: "bob" }, "c1"), say("Sent.")],
          approvers: [alice],
        }),
      },
      channels: { slack },
    });
    hosts.push(h.host);
    await h.host.ready();
    await post(h.host, hook("E1", [message("mail bob", "E1#0")]));
    await until(async () =>
      slack.performed.some((p) => JSON.stringify(p.op).includes("approval")),
    );
    const { db } = await storeConnection(h.store);
    const [row] = z
      .array(z.object({ thread_id: z.string() }))
      .parse(db.all("SELECT thread_id FROM channel_threads", []));
    const list = await (
      await h.call("GET", `/v1/threads/${row?.thread_id}/approvals`, {
        as: alice,
      })
    ).json();
    const granted = await h.call(
      "POST",
      `/v1/threads/${row?.thread_id}/approvals/${list[0].challenge_id}`,
      { as: alice, body: { decision: "grant" } },
    );
    expect(granted.status).toBe(200);
    await until(async () =>
      slack.performed.some((p) => JSON.stringify(p.op).includes("Sent.")),
    );
    expect(sends(await threadEvents(h.store))).toHaveLength(2);
  });
});
