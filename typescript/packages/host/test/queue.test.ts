import { afterEach, describe, expect, test } from "bun:test";
import { agent, scriptedModel, sqlite } from "@threads/core";
import {
  BranchId,
  openStore,
  storeConnection,
  tenantStore,
} from "@threads/core/host";
import { z } from "zod";
import { host } from "../src";
import {
  authenticate,
  eventsOf,
  fakeChannel,
  type Harness,
  harness,
  mailer,
  say,
  until,
  use,
  webhook,
} from "./kit";

// The channel inbox never loses or blocks an item (spec/schema/README.md, "Channel replies"): a
// decision is consumed with the append that applies it, a busy branch keeps it queued, it never
// waits behind a message the branch can't take, a handoff moves the conversation, and a reply
// begun before a crash is reconciled through the channel's lookup.

const TENANT = "slack:T1";
const alice = { issuer: "slack:T1", tenant: TENANT, subject: "U1" };

let h: Harness | undefined;
afterEach(async () => {
  await h?.host.stop();
  h = undefined;
});

function hook(delivery: string, item: Record<string, unknown>) {
  return webhook({
    tenant: TENANT,
    installation_id: "T1",
    delivery_id: delivery,
    items: [
      { principal: alice, address: "C1", item_key: `${delivery}#0`, ...item },
    ],
  });
}

const say_ = (text: string) => ({ kind: "message", content: text });

async function rows(harnessed: Harness) {
  const { db } = await storeConnection(harnessed.store);
  return z
    .array(z.object({ thread_id: z.string(), branch_id: z.string() }))
    .parse(
      db.all(
        "SELECT thread_id, branch_id FROM branches ORDER BY rowid LIMIT 1",
        [],
      ),
    )[0];
}

async function parkedOnApproval(sent: string[], extra: readonly unknown[]) {
  const slack = fakeChannel("support");
  h = harness({
    agents: {
      support: mailer({
        responses: [
          use("send_email", { to: "bob" }, "c1"),
          say("Sent."),
          ...extra,
        ],
        approvers: [alice],
        sent,
      }),
    },
    channels: { slack },
  });
  await h.host.ready();
  await h.call("POST", "/channels/slack/events", hook("E1", say_("mail bob")));
  await until(async () =>
    slack.performed.some((p) => JSON.stringify(p.op).includes("approval")),
  );
  const card = slack.performed.find((p) =>
    JSON.stringify(p.op).includes("approval"),
  );
  const challenge = z
    .object({ challenge_id: z.string() })
    .parse(card?.op).challenge_id;
  return { harnessed: h, challenge };
}

const grant = (challenge: string) => ({
  kind: "decision",
  challenge_id: challenge,
  decision: "grant",
});

describe("channel queue", () => {
  test("a click queued behind a message the parked branch can't take is applied", async () => {
    const sent: string[] = [];
    const { harnessed, challenge } = await parkedOnApproval(sent, [say("hi")]);
    await harnessed.call(
      "POST",
      "/channels/slack/events",
      hook("E2", say_("any update?")),
    );
    await harnessed.call(
      "POST",
      "/channels/slack/events",
      hook("E3", grant(challenge)),
    );
    await until(async () => sent.length === 1);
    expect(sent).toEqual(["bob"]);
  });

  test("a click while another process holds the lease stays queued, then applies", async () => {
    const sent: string[] = [];
    const { harnessed, challenge } = await parkedOnApproval(sent, []);
    const at = await rows(harnessed);
    const { log } = await openStore(tenantStore(harnessed.store, TENANT));
    const other = log.acquire(BranchId.parse(at?.branch_id), "other-process");
    if (!other.ok) throw new Error(other.error.message);
    await harnessed.call(
      "POST",
      "/channels/slack/events",
      hook("E2", grant(challenge)),
    );
    await Bun.sleep(200);
    const { db } = await storeConnection(harnessed.store);
    expect(
      db.all("SELECT consumed_seq FROM inbox WHERE item_key = 'E2#0'", []),
    ).toEqual([{ consumed_seq: null }]);
    other.value.release();
    await until(async () => sent.length === 1, 5_000);
    const granted = (
      await eventsOf(harnessed.store, TENANT, at?.branch_id ?? "")
    ).find((e) => e.type === "approval_granted");
    expect(
      db.all("SELECT consumed_seq FROM inbox WHERE item_key = 'E2#0'", []),
    ).toEqual([{ consumed_seq: granted?.["seq"] ?? -1 }]);
  });

  test("after a handoff the conversation continues with the target agent", async () => {
    const billing = agent({
      name: "billing",
      model: scriptedModel({
        responses: [say("billing: refund issued"), say("billing: again")],
      }),
    });
    const front = agent({
      name: "front",
      model: scriptedModel({
        responses: [use("handoff", { agent: "billing" }, "h1"), say("FRONT")],
      }),
      handoffs: [billing],
    });
    const slack = fakeChannel("support");
    h = harness({ agents: { support: front }, channels: { slack } });
    await h.host.ready();
    await h.call("POST", "/channels/slack/events", hook("E1", say_("charged")));
    await until(async () => slack.performed.length === 1);
    await h.call("POST", "/channels/slack/events", hook("E2", say_("more")));
    await until(async () => slack.performed.length === 2);
    expect(
      slack.performed.map((p) => z.object({ text: z.string() }).parse(p.op)),
    ).toEqual([{ text: "billing: refund issued" }, { text: "billing: again" }]);
  });

  test("a reply begun before a crash is reconciled by lookup, not parked", async () => {
    const store = sqlite(":memory:");
    // The first process hangs mid-send: it "crashes" after effect_begin.
    const first = {
      ...fakeChannel("support"),
      perform: () => new Promise<never>(() => {}),
    };
    const h1 = host({
      store,
      authenticate,
      agents: { support: mailer({ responses: [say("first reply")] }) },
      channels: { slack: first },
    });
    await h1.ready();
    const post = (
      target: ReturnType<typeof host>,
      d: ReturnType<typeof hook>,
    ) =>
      target.fetch(
        new Request("http://h/channels/slack/events", {
          method: "POST",
          body: JSON.stringify(d.body),
          headers: d.headers,
        }),
      );
    await post(h1, hook("E1", say_("hi")));
    const { db } = await storeConnection(store);
    await until(async () => db.all("SELECT 1 FROM branches", []).length > 0);
    const branch = z
      .array(z.object({ branch_id: z.string() }))
      .parse(db.all("SELECT branch_id FROM branches", []))[0]?.branch_id;
    await until(async () =>
      (await eventsOf(store, TENANT, branch ?? "")).some(
        (e) => e.type === "effect_begin",
      ),
    );
    db.run("DELETE FROM leases", []);
    const second = fakeChannel("support", { lookup: "final" });
    second.lookups.push("found");
    const h2 = host({
      store,
      authenticate,
      agents: { support: mailer({ responses: [say("second reply")] }) },
      channels: { slack: second },
    });
    await h2.ready();
    await post(h2, hook("E2", say_("are you there?")));
    await until(async () => second.performed.length === 1);
    const events = await eventsOf(store, TENANT, branch ?? "");
    expect(events.map((e) => e.type)).not.toContain("parked");
    expect(
      events.find((e) => e.type === "effect_resolved")?.["data"],
    ).toMatchObject({ call_id: "send_5_0", outcome: "confirmed_success" });
    await h2.stop();
  });
});
