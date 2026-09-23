import { afterEach, describe, expect, test } from "bun:test";
import { agent, scriptedModel, sqlite, tool } from "@threads/core";
import { openStore, tenantStore } from "@threads/core/host";
import { z } from "zod";
import { type Host, host } from "../src";
import { hostTicked } from "../src/host";
import { authenticate, fakeChannel, say, until, use, webhook } from "./kit";

// A host watches every channel thread it consumed an item on until the thread settles, and each
// tick runs a turn no process is running. A run still going in this process is never waited on
// there: other threads' recovery goes on, and stop() aborts the run instead of waiting behind it.

const TENANT = "slack:T1";
const alice = { issuer: "slack:T1", tenant: TENANT, subject: "U1" };

const hosts: Host[] = [];
afterEach(async () => {
  for (const h of hosts.splice(0)) await h.stop();
});

function post(h: Host, delivery: string, address: string): Promise<Response> {
  const d = webhook({
    tenant: TENANT,
    installation_id: "T1",
    delivery_id: delivery,
    items: [
      {
        kind: "message",
        principal: alice,
        address,
        item_key: `${delivery}#0`,
        content: "hi",
      },
    ],
  });
  return h.fetch(
    new Request("http://host.test/channels/slack/events", {
      method: "POST",
      headers: d.headers,
      body: JSON.stringify(d.body),
    }),
  );
}

describe("recovery around a live run", () => {
  test("another thread recovers while a run is going, and stop() aborts that run", async () => {
    let waiting = false;
    let aborted = false;
    // Runs until its run is aborted: a live run on a watched thread.
    const hold = tool({
      name: "hold",
      description: "Hold.",
      input: z.object({}),
      effect: "read_only",
      execute: async (_input, ctx) => {
        waiting = true;
        const { promise, resolve } = Promise.withResolvers<void>();
        ctx.signal.addEventListener("abort", () => resolve());
        await promise;
        aborted = true;
        return "stopped";
      },
    });
    const slack = fakeChannel("support");
    const store = sqlite(":memory:");
    const h = host({
      store,
      authenticate,
      agents: {
        support: agent({
          name: "support",
          model: scriptedModel({
            responses: [use("hold", {}, "h1"), say("C2 answered")],
          }),
          tools: [hold],
        }),
      },
      channels: { slack },
    });
    hosts.push(h);
    await h.ready();
    await post(h, "E1", "C1");
    await until(async () => waiting);
    // C2's run loses the race for its lease once: only a tick's recovery runs its turn.
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
    await post(h, "E2", "C2");
    await until(async () => lost);
    await until(async () => slack.performed.length === 1, 5_000);
    expect(slack.performed[0]?.op).toMatchObject({ text: "C2 answered" });
    expect(aborted).toBe(false);
    // A tick has looked at the live thread since: it didn't wait on the run.
    await hostTicked(h);
    const stopping = Date.now();
    await h.stop();
    expect(aborted).toBe(true);
    expect(Date.now() - stopping).toBeLessThan(1_000);
  }, 10_000);
});
