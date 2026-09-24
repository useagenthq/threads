import { afterEach, describe, expect, test } from "bun:test";
import { agent, scriptedModel, sqlite } from "@threads/core";
import {
  BranchId,
  type EventDraft,
  type KnownEvent,
  openStore,
  resumed,
  runEnd,
  storeConnection,
  tenantStore,
} from "@threads/core/host";
import { z } from "zod";
import { type Host, host } from "../src";
import { hostTicked } from "../src/host";
import {
  authenticate,
  type FakeChannel,
  fakeChannel,
  knownEventsOf,
  mailer,
  say,
  until,
  use,
  webhook,
} from "./kit";

// Invariant 3 for the host's own channel sends while an ask_user turn is open (review of lane
// 14A, plans/reviews/claude-14a-question-send-crash.ts): a question or correction send left in
// doubt by a crash is reconciled by the outbound path only, never recovered or dispatched by the
// agent's loop, and never holds up the answer on its way to the model.

type Store = ReturnType<typeof sqlite>;
const TENANT = "slack:T1";
const alice = { issuer: "slack:T1", tenant: TENANT, subject: "U1" };
const COLORS = { question: "Which color?", options: ["red", "blue"] };
const hosts: Host[] = [];
afterEach(async () => {
  for (const h of hosts.splice(0)) await h.stop();
});

const message = (text: string, key: string): unknown => ({
  kind: "message",
  principal: alice,
  address: "C1",
  item_key: key,
  content: text,
});
const hook = (delivery: string, items: readonly unknown[]) =>
  webhook({
    tenant: TENANT,
    installation_id: "T1",
    delivery_id: delivery,
    items,
  });

/** The channel's agent: the kit's mailer, or a team lead of that name with no members. */
function serving(responses: readonly unknown[], lead: boolean) {
  if (!lead) return mailer({ responses });
  const model = scriptedModel({ responses: [...responses] });
  return agent({ name: "support", model, team: [] });
}

function start(
  store: Store,
  responses: readonly unknown[],
  slack: FakeChannel,
  lead = false,
) {
  const h = host({
    store,
    authenticate,
    agents: { support: serving(responses, lead) },
    channels: { slack },
  });
  hosts.push(h);
  const post = (d: ReturnType<typeof hook>) =>
    h.fetch(
      new Request("http://host.test/channels/slack/events", {
        method: "POST",
        headers: { ...d.headers, "content-type": "application/json" },
        body: JSON.stringify(d.body),
      }),
    );
  return { h, post };
}

/** Records the op as landed, then never returns: the process dies mid-send. */
function crashingOn(n: number): FakeChannel {
  const base = fakeChannel("support", { buttons: false });
  let count = 0;
  return {
    ...base,
    perform: async (op, key, credentials) => {
      count++;
      if (count !== n) return base.perform(op, key, credentials);
      base.performed.push({ op, key });
      return new Promise(() => {});
    },
  };
}

async function branchOf(store: Store): Promise<BranchId> {
  const { db } = await storeConnection(store);
  const [row] = z
    .array(z.object({ branch_id: BranchId }))
    .parse(db.all("SELECT branch_id FROM branches", []));
  if (row === undefined) throw new Error("no branch");
  return row.branch_id;
}

async function log(store: Store): Promise<readonly KnownEvent[]> {
  const { db } = await storeConnection(store);
  if (db.all("SELECT branch_id FROM branches", []).length === 0) return [];
  return knownEventsOf(store, TENANT, await branchOf(store));
}

/** Appends under a lease of its own, as another process would. */
async function appendByHand(
  store: Store,
  drafts: (events: readonly KnownEvent[]) => readonly EventDraft[],
): Promise<void> {
  const { log: l } = await openStore(tenantStore(store, TENANT));
  const w = l.acquire(await branchOf(store), "test");
  if (!w.ok) throw new Error(w.error.message);
  try {
    for (const d of drafts(await log(store))) {
      const added = w.value.append([d]);
      if (!added.ok) throw new Error(added.error.message);
    }
  } finally {
    w.value.release();
  }
}

/** The asker's answer and its resumed, as a channel reply records them. */
async function answer(store: Store, text: string): Promise<void> {
  await appendByHand(store, () => [
    {
      type: "tool_result",
      type_version: 1,
      critical: true,
      actor: { kind: "user", principal: alice },
      data: {
        call_id: "q1",
        is_error: false,
        origin: "answered",
        completeness: "complete",
        preview: text,
      },
    },
  ]);
  await appendByHand(store, (events) => {
    const result = events.findLast((e) => e.type === "tool_result");
    if (result === undefined) throw new Error("no answer");
    return [resumed({ kind: "input", id: "q1" }, result.event_id)];
  });
}

const settled = (events: readonly KnownEvent[], callId: string) =>
  events.flatMap((e) =>
    e.type === "tool_result" && e.data.call_id === callId
      ? [`${e.data.origin}:${e.data.preview}`]
      : [],
  );

async function crashAfterQuestionBegun(
  store: Store,
  lead = false,
): Promise<void> {
  const ask = [use("ask_user", COLORS, "q1")];
  const first = start(store, ask, crashingOn(1), lead);
  await first.h.ready();
  await first.post(hook("E1", [message("Paint it.", "E1#0")]));
  await until(async () =>
    (await log(store)).some((e) => e.type === "effect_begin"),
  );
  await first.h.stop();
}

describe("a host send in doubt while a question is open", () => {
  test("begun, then answered: outbound reconciles it and the answer reaches the model", async () => {
    const store = sqlite(":memory:");
    await crashAfterQuestionBegun(store);
    await answer(store, "blue");
    const slack = fakeChannel("support", { buttons: false });
    slack.lookups.push("found");
    const next = start(store, [say("Blue it is.")], slack);
    await next.h.ready();
    await until(async () =>
      (await log(store)).some((e) => e.type === "turn_completed"),
    );
    const events = await log(store);
    expect(settled(events, "question_q1_0")).toEqual([
      expect.stringMatching(/^executed:sent found-/),
    ]);
    expect(events.some((e) => e.type === "effect_unknown")).toBe(false);
    expect(events.findLast((e) => e.type === "turn_completed")).toMatchObject({
      data: { reason: "end_turn" },
    });
    expect(slack.performed.map((p) => p.key)).toEqual([
      expect.stringMatching(/:send_\d+_0$/),
    ]);
  });

  test("issued but never begun, then answered: never sent, closed not_executed, no approval", async () => {
    const store = sqlite(":memory:");
    const dying: FakeChannel = {
      ...fakeChannel("support", { buttons: false }),
      renderText: () => {
        throw new Error("died before issuing");
      },
    };
    const first = start(store, [use("ask_user", COLORS, "q1")], dying);
    await first.h.ready();
    await first.post(hook("E1", [message("Paint it.", "E1#0")]));
    await until(async () =>
      (await log(store)).some((e) => e.type === "parked"),
    );
    await first.h.stop();
    await appendByHand(store, (events) => {
      const request = events.findLast((e) => e.type === "model_request");
      if (request === undefined) throw new Error("no request");
      return [
        {
          type: "tool_call",
          type_version: 1,
          critical: true,
          actor: { kind: "host" },
          data: {
            call_id: "question_q1_0",
            name: "channel_send",
            input: { kind: "text", text: "Which color?", address: "C1" },
            request_event_id: request.event_id,
          },
        },
        {
          type: "permission_decision",
          type_version: 1,
          critical: true,
          actor: { kind: "host" },
          data: {
            call_id: "question_q1_0",
            decision: "allow",
            source: "policy",
            rule_id: "channel_delivery",
          },
        },
      ];
    });
    await answer(store, "blue");
    const slack = fakeChannel("support", { buttons: false });
    const next = start(store, [say("Blue it is.")], slack);
    await next.h.ready();
    await until(async () =>
      (await log(store)).some((e) => e.type === "turn_completed"),
    );
    for (let i = 0; i < 2; i++) await hostTicked(next.h);
    const events = await log(store);
    expect(settled(events, "question_q1_0")).toEqual([
      "not_executed:not sent: no longer due",
    ]);
    expect(
      events.some(
        (e) => e.type === "effect_begin" && e.data.call_id === "question_q1_0",
      ),
    ).toBe(false);
    expect(events.some((e) => e.type === "approval_requested")).toBe(false);
    expect(slack.performed.map((p) => p.key)).toEqual([
      expect.stringMatching(/:send_\d+_0$/),
    ]);
  });

  test("a correction begun, then a restart: one correction, the question still answers", async () => {
    const store = sqlite(":memory:");
    const first = start(store, [use("ask_user", COLORS, "q1")], crashingOn(2));
    await first.h.ready();
    await first.post(hook("E1", [message("Paint it.", "E1#0")]));
    await until(async () =>
      (await log(store)).some((e) => e.type === "parked"),
    );
    await first.post(hook("E2", [message("green", "E2#0")]));
    await until(
      async () =>
        (await log(store)).filter((e) => e.type === "effect_begin").length ===
        2,
    );
    await first.h.stop();
    const slack = fakeChannel("support", { buttons: false });
    slack.lookups.push("found");
    const next = start(store, [say("Red then.")], slack);
    await next.h.ready();
    for (let i = 0; i < 3; i++) await hostTicked(next.h);
    expect(slack.performed).toHaveLength(0);
    await next.post(hook("E3", [message("red", "E3#0")]));
    await until(async () => slack.performed.length === 1);
    const events = await log(store);
    expect(
      events.filter(
        (e) => e.type === "tool_call" && e.data.name === "channel_send",
      ),
    ).toHaveLength(3);
    expect(events.some((e) => e.type === "effect_unknown")).toBe(false);
  });

  test.each([
    ["an agent", false],
    ["a team lead", true],
  ])(
    "begun, no adapter lookup: the send parks for a human, the asker answers, %s's run completes",
    async (_, lead) => {
      const store = sqlite(":memory:");
      await crashAfterQuestionBegun(store, lead);
      const slack = fakeChannel("support", { buttons: false, lookup: "none" });
      const next = start(store, [say("Blue it is.")], slack, lead);
      await next.h.ready();
      await until(async () =>
        (await log(store)).some(
          (e) => e.type === "parked" && e.data.address.kind === "effect",
        ),
      );
      await next.post(hook("E2", [message("2", "E2#0")]));
      await until(async () =>
        (await log(store)).some((e) => e.type === "turn_completed"),
      );
      const events = await log(store);
      const input = events.find((e) => e.type === "user_input");
      if (input === undefined) throw new Error("no input");
      expect(runEnd(events, input.event_id).status).toBe("completed");
      expect(settled(events, "q1")).toEqual(["answered:blue"]);
    },
  );
});
