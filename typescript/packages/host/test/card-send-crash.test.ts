import { afterEach, describe, expect, test } from "bun:test";
import { openThread, sqlite } from "@threads/core";
import {
  BranchId,
  type KnownEvent,
  storeConnection,
  ThreadId,
  tenantStore,
} from "@threads/core/host";
import { z } from "zod";
import { type Host, host } from "../src";
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

// An approval card is the host's own send too (spec/schema/README.md, "The host's calls are the
// host's"): a card left in doubt by a crash is reconciled by outbound after its challenge is
// decided, never recovered by the agent's loop, and the approved call runs once.

type Store = ReturnType<typeof sqlite>;
const TENANT = "slack:T1";
const alice = { issuer: "slack:T1", tenant: TENANT, subject: "U1" };
const hosts: Host[] = [];
afterEach(async () => {
  for (const h of hosts.splice(0)) await h.stop();
});

const hook = (delivery: string, text: string) =>
  webhook({
    tenant: TENANT,
    installation_id: "T1",
    delivery_id: delivery,
    items: [
      {
        kind: "message",
        principal: alice,
        address: "C1",
        item_key: `${delivery}#0`,
        content: text,
      },
    ],
  });

/** A host whose scripted model answers with `responses`, from where the last host left off. */
function start(
  store: Store,
  slack: FakeChannel,
  sent: string[],
  responses: readonly unknown[],
) {
  const h = host({
    store,
    authenticate,
    agents: { support: mailer({ responses, sent }) },
    channels: { slack },
  });
  hosts.push(h);
  return h;
}

async function thread(store: Store) {
  const { db } = await storeConnection(store);
  const [row] = z
    .array(z.object({ thread_id: ThreadId, branch_id: BranchId }))
    .parse(db.all("SELECT thread_id, branch_id FROM branches", []));
  if (row === undefined) throw new Error("no branch");
  return row;
}

async function log(store: Store): Promise<readonly KnownEvent[]> {
  const { db } = await storeConnection(store);
  if (db.all("SELECT branch_id FROM branches", []).length === 0) return [];
  return knownEventsOf(store, TENANT, (await thread(store)).branch_id);
}

describe("an approval card send in doubt", () => {
  test("begun, then the challenge is granted: the card is reconciled and the email sent once", async () => {
    const store = sqlite(":memory:");
    const dying = fakeChannel("support", { buttons: true });
    const crashing: FakeChannel = {
      ...dying,
      perform: async (op, key) => {
        dying.performed.push({ op, key });
        return new Promise(() => {});
      },
    };
    const sent: string[] = [];
    const ask = [use("send_email", { to: "bob" }, "c1")];
    const first = start(store, crashing, sent, ask);
    await first.ready();
    await first.fetch(
      new Request("http://host.test/channels/slack/events", {
        method: "POST",
        headers: {
          ...hook("E1", "Email bob.").headers,
          "content-type": "application/json",
        },
        body: JSON.stringify(hook("E1", "Email bob.").body),
      }),
    );
    await until(async () =>
      (await log(store)).some((e) => e.type === "effect_begin"),
    );
    await first.stop();
    const { thread_id } = await thread(store);
    const handle = await openThread(tenantStore(store, TENANT), thread_id);
    if (!handle.ok) throw new Error(handle.error.message);
    const asked = (await log(store)).find(
      (e) => e.type === "approval_requested",
    );
    if (asked?.type !== "approval_requested") throw new Error("no challenge");
    const granted = await handle.value.approve(asked.data.challenge_id, alice);
    expect(granted.ok).toBe(true);
    const slack = fakeChannel("support", { buttons: true });
    slack.lookups.push("found");
    const next = start(store, slack, sent, [say("Sent.")]);
    await next.ready();
    await until(async () =>
      (await log(store)).some((e) => e.type === "turn_completed"),
    );
    const events = await log(store);
    const card = events.find(
      (e) =>
        e.type === "tool_result" &&
        e.data.call_id.startsWith("send_") &&
        e.data.preview.startsWith("sent found-"),
    );
    expect(card).toBeDefined();
    expect(events.some((e) => e.type === "effect_unknown")).toBe(false);
    expect(sent).toEqual(["bob"]);
  });
});
