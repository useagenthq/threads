import { afterEach, describe, expect, test } from "bun:test";
import {
  agent,
  type Model,
  openTeam,
  scriptedModel,
  sqlite,
} from "@threads/core";
import {
  type KnownEvent,
  type Principal,
  type Store,
  storeConnection,
} from "@threads/core/host";
import { z } from "zod";
import type { TeamId } from "../../core/src/log";
import { cleanup, expireLeases, stall } from "./api-kit";
import {
  alice,
  type FakeChannel,
  fakeChannel,
  type Harness,
  harness,
  knownEventsOf,
  say,
  until,
  use,
  webhook,
} from "./kit";
import { sqlAll } from "./sql";
import { assertReplays, teamOf } from "./team-kit";

// The host wakes an idle thread (Gate 1 §2.7.2 and §2.7.3; design §7 Phase 2 A). Mail a member
// sends after the lead's run has ended is consumed by the host's team tick, which opens a turn of
// the request the mail belongs to, and the host runs that turn on: no new run is started, and on
// a channel thread the woken turn's answer is delivered as a further reply. A background child a
// crash stopped is woken the same way, through its pending_wakes row.

/** Every host a test started, stopped when it ends: a dead one left ticking spams every test. */
const hosts: Harness[] = [];
const served = (options: Parameters<typeof harness>[0]): Harness => {
  const made = harness(options);
  hosts.push(made);
  return made;
};

afterEach(async () => {
  await cleanup();
  for (const one of hosts.splice(0)) await one.host.stop();
});

const TENANT = "slack:T1";
const chatter: Principal = {
  issuer: "slack:T1",
  tenant: TENANT,
  subject: "U1",
};

/** A helper that sends one message to `to` and then reports; its turn ends idle. */
const helper = (to: string, text: string): Model =>
  scriptedModel({
    responses: [use("send", { to, text }, "s1"), say("Reported.")],
  });

/** The operator starts `named` on the lead's team, as another process would. */
async function operatorStarts(
  store: Store,
  team: TeamId,
  who: Principal,
  named: string,
): Promise<void> {
  const opened = await openTeam(
    store,
    { tenant: who.tenant, id: team },
    { principal: who },
  );
  if (!opened.ok) throw new Error(opened.error.message);
  const started = await opened.value.start(named, "Scan the deps.");
  if (started.status !== "started")
    throw new Error(`the start was ${started.status}`);
}

const turns = (events: readonly KnownEvent[]): number =>
  events.filter((e) => e.type === "turn_completed").length;

/** One message from the chatter, through the fake channel's webhook. */
async function says(served: Harness, text: string): Promise<void> {
  await served.call(
    "POST",
    "/channels/slack/events",
    webhook({
      tenant: TENANT,
      installation_id: "T1",
      delivery_id: "E1",
      items: [
        {
          kind: "message",
          principal: chatter,
          address: "C1",
          item_key: "E1#0",
          content: text,
        },
      ],
    }),
  );
}

/** The texts a channel adapter was asked to deliver, in order. */
const delivered = (channel: FakeChannel): readonly string[] =>
  channel.performed.map((p) => z.object({ text: z.string() }).parse(p.op).text);

/** Every text a model answered on the branch, in order. */
const answers = (events: readonly KnownEvent[]): readonly string[] =>
  events.flatMap((e) =>
    e.type === "model_response"
      ? e.data.content.flatMap((p) => (p.type === "text" ? [p.text] : []))
      : [],
  );

describe("a member's send after the run ended", () => {
  test("wakes the hosted lead, with no new run", async () => {
    const lead = agent({
      name: "wake_lead",
      model: scriptedModel({
        responses: [say("Standing by."), say("The scan is clean.")],
      }),
      team: [
        agent({
          name: "wake_helper",
          model: helper("wake_lead", "The scan is clean."),
        }),
      ],
    });
    const h = served({ agents: { lead } });
    const store = h.store;
    await h.host.ready();
    const run = await h.host.startRun(
      { agent: "lead", input: "Stand by." },
      { principal: alice, idempotencyKey: "k-1" },
    );
    if (!run.ok) throw new Error(run.error.message);
    const branch = run.value.branch_id;
    const events = () => knownEventsOf(store, alice.tenant, branch);
    await until(async () => turns(await events()) === 1);
    const team = await teamOf(store, alice.tenant, branch);

    await operatorStarts(store, team, alice, "wake_helper");
    await until(async () => turns(await events()) === 2, 10_000);

    const all = await events();
    // The turn the mail opened is not a new run: the lead's log holds one user_input.
    expect(all.filter((e) => e.type === "user_input")).toHaveLength(1);
    const openers = all.filter(
      (e) => e.type === "message_received" || e.type === "turn_completed",
    );
    expect(openers[1]?.type).toBe("message_received");
    expect(answers(all).at(-1)).toBe("The scan is clean.");
    await assertReplays(store, alice.tenant, team);
  }, 20_000);
});

describe("a team lead on a channel thread", () => {
  test("the woken turn's answer is a further reply", async () => {
    const slack: FakeChannel = fakeChannel("lead");
    const lead = agent({
      name: "chat_lead",
      model: scriptedModel({
        responses: [say("Looking into it."), say("All clear.")],
      }),
      team: [
        agent({
          name: "chat_helper",
          model: helper("chat_lead", "All clear."),
        }),
      ],
    });
    const h = served({ agents: { lead }, channels: { slack } });
    const store = h.store;
    await h.host.ready();
    await says(h, "Is the scan done?");
    await until(async () => slack.performed.length === 1);
    const branch = await mainBranch(store);
    const team = await teamOf(store, TENANT, branch);

    await operatorStarts(store, team, chatter, "chat_helper");
    await until(async () => slack.performed.length === 2, 10_000);

    expect(delivered(slack)).toEqual(["Looking into it.", "All clear."]);
    const all = await knownEventsOf(store, TENANT, branch);
    expect(all.filter((e) => e.type === "user_input")).toHaveLength(1);
    expect(answers(all)).toEqual(["Looking into it.", "All clear."]);
    await assertReplays(store, TENANT, team);
  }, 20_000);

  test("a crash before the woken answer is delivered leaves one delivery", async () => {
    // The dead host says what it could not render on every tick; the drill expects it.
    const said = console.error;
    console.error = () => undefined;
    try {
      await deliveredOnce();
    } finally {
      console.error = said;
    }
  }, 30_000);
});

/** The drill: the woken answer is rendered by the next host, and delivered once. */
async function deliveredOnce(): Promise<void> {
  const store = sqlite(":memory:");
  const lead = agent({
    name: "crash_lead",
    model: scriptedModel({
      responses: [say("Looking into it."), say("All clear.")],
    }),
    team: [
      agent({
        name: "crash_helper",
        model: helper("crash_lead", "All clear."),
      }),
    ],
  });
  // The dead host renders every reply but the woken turn's: its send never begins.
  const dead = fakeChannel("lead");
  const render = dead.render;
  const deaf: FakeChannel = {
    ...dead,
    render: (event) => {
      if (
        event.type === "model_response" &&
        answers([event])[0] === "All clear."
      )
        throw new Error("the host died before delivery");
      return render(event);
    },
  };
  const crashed = served({
    store,
    agents: { lead },
    channels: { slack: deaf },
  });
  await crashed.host.ready();
  await says(crashed, "Is the scan done?");
  await until(async () => deaf.performed.length === 1);
  const branch = await mainBranch(store);
  const team = await teamOf(store, TENANT, branch);
  const events = () => knownEventsOf(store, TENANT, branch);
  await operatorStarts(store, team, chatter, "crash_helper");
  // The woken turn answers, and the reply it derives is never delivered.
  await until(async () => turns(await events()) === 2, 10_000);
  expect(delivered(deaf)).toEqual(["Looking into it."]);
  await expireLeases(store);

  const slack = fakeChannel("lead");
  const h = served({ store, agents: { lead }, channels: { slack } });
  await h.host.ready();
  await until(async () => slack.performed.length === 1, 10_000);
  // Once, and only once: the next host derives the same reply from the log.
  await until(async () =>
    (await events()).some((e) => e.type === "effect_commit"),
  );
  expect(delivered(slack)).toEqual(["All clear."]);
  const sends = (await events()).filter(
    (e) => e.type === "tool_call" && e.data.name === "channel_send",
  );
  expect(sends).toHaveLength(2);
  expect(turns(await events())).toBe(2);
  await assertReplays(store, TENANT, team);
  // Stopped here, while console.error is still silenced: its next tick would say it again.
  await crashed.host.stop();
}

describe("a channel thread whose background child a crash stopped", () => {
  test("the next host wakes it, and the answer is delivered", async () => {
    const store = sqlite(":memory:");
    const scan = use(
      "spawn_agent",
      { agent: "scanner", prompt: "Scan.", background: true },
      "c1",
    );
    const support = (lead: Model, child: Model) =>
      agent({
        name: "support",
        model: lead,
        subagents: [agent({ name: "scanner", model: child })],
      });
    const dead = fakeChannel("support");
    const crashed = served({
      store,
      agents: {
        support: support(
          scriptedModel({ responses: [scan, say("Started.")] }),
          stall(),
        ),
      },
      channels: { slack: dead },
    });
    await crashed.host.ready();
    await crashed.call(
      "POST",
      "/channels/slack/events",
      webhook({
        tenant: TENANT,
        installation_id: "T1",
        delivery_id: "E1",
        items: [
          {
            kind: "message",
            principal: chatter,
            address: "C1",
            item_key: "E1#0",
            content: "Scan in the background.",
          },
        ],
      }),
    );
    // The turn answers, but its run waits for the background child, so nothing is delivered.
    await until(async () => (await branches(store)).length > 0);
    const branch = await mainBranch(store);
    await until(async () =>
      (await knownEventsOf(store, TENANT, branch)).some(
        (e) => e.type === "turn_completed",
      ),
    );
    expect(dead.performed).toEqual([]);
    // The crash: the child never reported, and the dead host's leases run out.
    await expireLeases(store);

    const slack = fakeChannel("support");
    const h = served({
      store,
      agents: {
        support: support(
          scriptedModel({ responses: [say("The scan is clean.")] }),
          scriptedModel({ responses: [say("No vulnerable deps.")] }),
        ),
      },
      channels: { slack },
    });
    await h.host.ready();
    await until(async () => slack.performed.length === 2, 15_000);

    const all = await knownEventsOf(store, TENANT, branch);
    expect(all.filter((e) => e.type === "woken")).toHaveLength(1);
    expect(all.filter((e) => e.type === "user_input")).toHaveLength(1);
    expect(
      slack.performed.map(
        (p) => z.object({ text: z.string() }).parse(p.op).text,
      ),
    ).toEqual(["Started.", "The scan is clean."]);
  }, 30_000);
});

/** Every root branch in the store, oldest first. */
async function branches(store: Store): Promise<readonly string[]> {
  const { db } = await storeConnection(store);
  return z
    .array(z.object({ branch_id: z.string() }))
    .parse(
      await sqlAll(
        db,
        "SELECT branch_id FROM branches WHERE parent_branch_id IS NULL ORDER BY branch_id",
      ),
    )
    .map((r) => r.branch_id);
}

/** The one root branch in the store: the conversation's thread. */
async function mainBranch(store: Store): Promise<string> {
  const first = (await branches(store))[0];
  if (first === undefined) throw new Error("no branch");
  return first;
}
