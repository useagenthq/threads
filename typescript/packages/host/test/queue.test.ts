import { afterEach, describe, expect, test } from "bun:test";
import { agent, extension, scriptedModel, sqlite } from "@threads/core";
import {
  BranchId,
  openStore,
  type StoreDriver,
  storeConnection,
  ThreadId,
  tenantStore,
} from "@threads/core/host";
import { z } from "zod";
import { host } from "../src";
import { hostTicked } from "../src/host";
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
import { sqlAll, sqlRun } from "./sql";

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

const e2Consumed = (db: StoreDriver) =>
  sqlAll(db, "SELECT consumed_seq FROM inbox WHERE item_key = 'E2#0'");

/** A webhook to a host with no harness around it (several hosts on one store). */
const post = (target: ReturnType<typeof host>, d: ReturnType<typeof hook>) =>
  target.fetch(
    new Request("http://h/channels/slack/events", {
      method: "POST",
      body: JSON.stringify(d.body),
      headers: d.headers,
    }),
  );

async function rows(harnessed: Harness) {
  const { db } = await storeConnection(harnessed.store);
  return z
    .array(z.object({ thread_id: z.string(), branch_id: z.string() }))
    .parse(
      await sqlAll(
        db,
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
    const other = await log.acquire(
      BranchId.parse(at?.branch_id),
      "other-process",
    );
    if (!other.ok) throw new Error(other.error.message);
    await harnessed.call(
      "POST",
      "/channels/slack/events",
      hook("E2", grant(challenge)),
    );
    await Bun.sleep(200);
    const { db } = await storeConnection(harnessed.store);
    expect(await e2Consumed(db)).toEqual([{ consumed_seq: null }]);
    await other.value.release();
    await until(async () => sent.length === 1, 5_000);
    const granted = (
      await eventsOf(harnessed.store, TENANT, at?.branch_id ?? "")
    ).find((e) => e.type === "approval_granted");
    expect(await e2Consumed(db)).toEqual([
      { consumed_seq: granted?.["seq"] ?? -1 },
    ]);
  });

  test("a message whose thread another host has just created, still empty, is not dropped", async () => {
    const slack = fakeChannel("support");
    h = harness({
      agents: { support: mailer({ responses: [say("hi back")] }) },
      channels: { slack },
    });
    // Received before ready(): durable in the inbox, not consumed yet.
    await h.call("POST", "/channels/slack/events", hook("E1", say_("hi")));
    const { db } = await storeConnection(h.store);
    const [queued] = z
      .array(z.object({ thread_id: ThreadId }))
      .parse(await sqlAll(db, "SELECT thread_id FROM inbox", []));
    // Another host made the thread's root and has not yet appended its thread_started.
    const { log } = await openStore(tenantStore(h.store, TENANT));
    const made = await log.createBranch(
      ThreadId.parse(queued?.thread_id),
      BranchId.parse(crypto.randomUUID()),
    );
    if (!made.ok) throw new Error(made.error.message);
    await h.host.ready();
    await until(async () => slack.performed.length === 1);
    expect(
      await sqlAll(db, "SELECT consumed_seq > 0 AS applied FROM inbox", []),
    ).toEqual([{ applied: 1 }]);
  });

  test("an item a host can't route stays queued for a host that can", async () => {
    const store = sqlite(":memory:");
    const slack = fakeChannel("support");
    // Another deployment on the same store: it doesn't serve this channel.
    const other = host({
      store,
      authenticate,
      agents: { support: mailer({ responses: [] }) },
    });
    const serving = host({
      store,
      authenticate,
      agents: { support: mailer({ responses: [say("hi back")] }) },
      channels: { slack },
    });
    await post(serving, hook("E1", say_("hi")));
    await other.ready();
    // Past the other host's first tick, which sweeps every pending row.
    await hostTicked(other);
    const { db } = await storeConnection(store);
    expect(await sqlAll(db, "SELECT consumed_seq FROM inbox", [])).toEqual([
      { consumed_seq: null },
    ]);
    await serving.ready();
    await until(async () => slack.performed.length === 1, 5_000);
    await other.stop();
    await serving.stop();
  });

  test("an item for a thread pinned to another config stays queued for the host that has it", async () => {
    const store = sqlite(":memory:");
    const support = (instructions: string, text: string) =>
      agent({
        name: "support",
        instructions,
        model: scriptedModel({ responses: [say(text)] }),
      });
    const first = fakeChannel("support");
    const serving = host({
      store,
      authenticate,
      agents: { support: support("Be brief.", "first") },
      channels: { slack: first },
    });
    await serving.ready();
    await post(serving, hook("E1", say_("hi")));
    await until(async () => first.performed.length === 1);
    await serving.stop();
    // Same agent name, another config: this host can't continue the thread.
    const other = fakeChannel("support");
    const changed = host({
      store,
      authenticate,
      agents: { support: support("Be verbose.", "second") },
      channels: { slack: other },
    });
    await changed.ready();
    await post(changed, hook("E2", say_("again")));
    await hostTicked(changed);
    await hostTicked(changed);
    const { db } = await storeConnection(store);
    expect(await e2Consumed(db)).toEqual([{ consumed_seq: null }]);
    expect(other.performed).toHaveLength(0);
    await changed.stop();
  });

  test("a stop_when_idle sent mid-run is appended with its consumption and cancels no child", async () => {
    const gate = Promise.withResolvers<void>();
    // The child's turn goes on only once released: until then it and the lead's run are going.
    const held = extension({
      name: "held",
      hooks: {
        beforeModel: async () => {
          await gate.promise;
          return { decision: "proceed" };
        },
      },
    });
    const worker = agent({
      name: "worker",
      model: scriptedModel({ responses: [say("Ok.")] }),
      extensions: [held],
    });
    const lead = agent({
      name: "lead",
      model: scriptedModel({
        responses: [
          use("spawn_agent", { agent: "worker", prompt: "Wait." }, "s1"),
          say("Done."),
        ],
      }),
      subagents: [worker],
    });
    const at = harness({
      agents: { lead },
      channels: { slack: fakeChannel("lead") },
    });
    h = at;
    await at.host.ready();
    await at.call("POST", "/channels/slack/events", hook("E1", say_("go")));
    const { db } = await storeConnection(at.store);
    const branches = async () =>
      z
        .array(z.object({ branch_id: z.string() }))
        .parse(
          await sqlAll(db, "SELECT branch_id FROM branches ORDER BY rowid", []),
        )
        .map((r) => r.branch_id);
    const logOf = (branch: string | undefined) =>
      eventsOf(at.store, TENANT, branch ?? "");
    await until(
      async () =>
        (await branches()).length === 2 &&
        (await logOf((await branches())[1])).some(
          (e) => e.type === "user_input",
        ),
    );
    const stop = { kind: "control", command: "stop_when_idle" };
    await at.call("POST", "/channels/slack/events", hook("E2", stop));
    const consumedAt = async () =>
      z
        .array(z.object({ consumed_seq: z.number().nullable() }))
        .parse(await e2Consumed(db))[0]?.consumed_seq;
    await until(async () => typeof (await consumedAt()) === "number");
    const [root, child] = await branches();
    const stopped = (await logOf(root)).find(
      (e) => e.type === "stop_when_idle",
    );
    expect(stopped?.["seq"]).toBe(await consumedAt());
    expect((await logOf(child)).map((e) => e.type)).not.toContain(
      "cancel_requested",
    );
    gate.resolve();
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
    await post(h1, hook("E1", say_("hi")));
    const { db } = await storeConnection(store);
    await until(
      async () => (await sqlAll(db, "SELECT 1 FROM branches", [])).length > 0,
    );
    const branch = z
      .array(z.object({ branch_id: z.string() }))
      .parse(
        await sqlAll(db, "SELECT branch_id FROM branches", []),
      )[0]?.branch_id;
    await until(async () =>
      (await eventsOf(store, TENANT, branch ?? "")).some(
        (e) => e.type === "effect_begin",
      ),
    );
    await sqlRun(db, "DELETE FROM leases", []);
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
