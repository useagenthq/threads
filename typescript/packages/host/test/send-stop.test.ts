import { afterEach, describe, expect, test } from "bun:test";
import { agent, type Store, scriptedModel, sqlite } from "@threads/core";
import { sandboxFetch } from "@threads/core/adapter";
import { openStore, storeConnection, tenantStore } from "@threads/core/host";
import { z } from "zod";
import { type Host, host } from "../src";
import { hostTicked } from "../src/host";
import {
  authenticate,
  eventsOf,
  fakeChannel,
  say,
  until,
  webhook,
} from "./kit";

// stop() and a send in flight (invariant 3): a send whose request has passed the transport fence
// keeps its lease until it settles, so no other host can look it up as not sent and send again
// while it may still land; a send not yet at the fence is refused there once the host stops.

const TENANT = "slack:T1";
const alice = { issuer: "slack:T1", tenant: TENANT, subject: "U1" };

const hosts: Host[] = [];
afterEach(async () => {
  for (const h of hosts.splice(0)) await h.stop();
});

/** A channel whose send waits on `gate`: before its request reaches the fence, or after. */
function gated(where: "before" | "after") {
  const gate = Promise.withResolvers<void>();
  const at = Promise.withResolvers<void>();
  const landed: string[] = [];
  const refused: string[] = [];
  const channel = {
    ...fakeChannel("support"),
    perform: async (_op: unknown, key: string) => {
      const send = sandboxFetch(async () => {
        if (where === "after") {
          at.resolve();
          await gate.promise;
        }
        landed.push(key);
        return new Response();
      });
      if (where === "before") {
        at.resolve();
        await gate.promise;
      }
      try {
        await send("fake://send");
      } catch (error) {
        refused.push(key);
        throw error;
      }
      return { status: "sent" as const, platform_ref: `ref-${key}` };
    },
  };
  return { channel, gate, at: at.promise, landed, refused };
}

async function started(
  channel: ReturnType<typeof gated>["channel"],
  store: Store,
): Promise<Host> {
  const h = host({
    store,
    authenticate,
    agents: {
      support: agent({
        name: "support",
        model: scriptedModel({ responses: [say("hello")] }),
      }),
    },
    channels: { slack: channel },
  });
  await h.ready();
  const d = webhook({
    tenant: TENANT,
    installation_id: "T1",
    delivery_id: "E1",
    items: [
      {
        kind: "message",
        principal: alice,
        address: "C1",
        item_key: "E1#0",
        content: "hi",
      },
    ],
  });
  await h.fetch(
    new Request("http://host.test/channels/slack/events", {
      method: "POST",
      headers: d.headers,
      body: JSON.stringify(d.body),
    }),
  );
  return h;
}

describe("stop() with a send in flight", () => {
  test("a send past the fence keeps its lease until it settles", async () => {
    const s = gated("after");
    const store = sqlite(":memory:");
    const h = await started(s.channel, store);
    await s.at;
    let stopped = false;
    const stopping = (async () => {
      await h.stop();
      stopped = true;
    })();
    // Past the fence, the request may still land: stop() waits and the lease stays held.
    await Bun.sleep(200);
    expect(stopped).toBe(false);
    s.gate.resolve();
    await stopping;
    expect(s.landed).toHaveLength(1);
    // Settled under its own lease: nothing is left in doubt for another host.
    const { db } = await storeConnection(store);
    const [row] = z
      .array(z.object({ branch_id: z.string() }))
      .parse(db.all("SELECT branch_id FROM branches", []));
    const types = (await eventsOf(store, TENANT, row?.branch_id ?? "")).map(
      (e) => e.type,
    );
    expect(types).toContain("effect_commit");
  });

  test("a send not yet at the fence is refused there once the host stops", async () => {
    const s = gated("before");
    const h = await started(s.channel, sqlite(":memory:"));
    await s.at;
    await h.stop();
    s.gate.resolve();
    await until(async () => s.refused.length === 1);
    expect(s.landed).toHaveLength(0);
  });
});

describe("a send held past the fence for longer than the lease lasts", () => {
  test("keeps its lease, so another host never sends it again", async () => {
    const store = sqlite(":memory:");
    // Every lease lasts 300 ms here: a send held for seconds outlives many of them.
    const { log } = await openStore(tenantStore(store, TENANT));
    const acquire = log.acquire.bind(log);
    log.acquire = (branch, holder) => acquire(branch, holder, 300);
    const s = gated("after");
    const first = await started(s.channel, store);
    hosts.push(first);
    await s.at;
    // Another host on the store, whose recovery looks at the thread on every tick.
    const other = fakeChannel("support");
    const second = host({
      store,
      authenticate,
      agents: {
        support: agent({
          name: "support",
          model: scriptedModel({ responses: [] }),
        }),
      },
      channels: { slack: other },
    });
    hosts.push(second);
    await second.ready();
    await hostTicked(second);
    await hostTicked(second);
    s.gate.resolve();
    await until(async () => s.landed.length === 1);
    await hostTicked(second);
    expect(other.performed).toHaveLength(0);
  }, 15_000);
});
