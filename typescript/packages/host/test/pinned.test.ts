import { afterEach, describe, expect, test } from "bun:test";
import { agent, scriptedModel } from "@threads/core";
import {
  BranchId,
  hostRunner,
  knownEvents,
  openStore,
  storeConnection,
  ThreadId,
  tenantStore,
} from "@threads/core/host";
import { z } from "zod";
import { hostTicked } from "../src/host";
import { fakeChannel, type Harness, harness, say, until, webhook } from "./kit";

// A thread is continued only with the config it was started with (config_hash), and that is
// decided under the branch lease, in the transaction that appends the input and consumes it.

const TENANT = "slack:T1";
const alice = { issuer: "slack:T1", tenant: TENANT, subject: "U1" };

let h: Harness | undefined;
afterEach(async () => {
  await h?.host.stop();
  h = undefined;
});

const support = (instructions: string) =>
  agent({
    name: "support",
    instructions,
    model: scriptedModel({ responses: [say("hi back")] }),
  });

describe("the pinned config", () => {
  test("a config another host pins between the check and the lease leaves the item queued", async () => {
    const slack = fakeChannel("support");
    const at = harness({
      agents: { support: support("Be brief.") },
      channels: { slack },
    });
    h = at;
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
    await at.call("POST", "/channels/slack/events", d);
    const { db } = await storeConnection(at.store);
    const [queued] = z
      .array(z.object({ thread_id: ThreadId }))
      .parse(db.all("SELECT thread_id FROM inbox", []));
    const threadId = ThreadId.parse(queued?.thread_id);
    // An empty root: this host's check sees no pinned config yet.
    const { log } = await openStore(tenantStore(at.store, TENANT));
    const branch = BranchId.parse(crypto.randomUUID());
    const made = log.createBranch(threadId, branch);
    if (!made.ok) throw new Error(made.error.message);
    // Right before this host takes the lease, another host pins another config.
    const other = hostRunner(support("Be verbose."));
    if (other === undefined) throw new Error("agent() registers a runner");
    const pinned = await other.started();
    const acquire = log.acquire.bind(log);
    let raced = false;
    log.acquire = (b, holder, ttl) => {
      if (!raced && holder.startsWith("host-")) {
        raced = true;
        const w = acquire(b, "other-host", ttl);
        if (!w.ok) throw new Error(w.error.message);
        w.value.append([pinned]);
        w.value.release();
      }
      return acquire(b, holder, ttl);
    };
    await at.host.ready();
    await until(async () => raced);
    await hostTicked(at.host);
    expect(db.all("SELECT consumed_seq FROM inbox", [])).toEqual([
      { consumed_seq: null },
    ]);
    const read = log.read(branch);
    if (!read.ok) throw new Error(read.error.message);
    expect(knownEvents(read.value).map((e) => e.type)).toEqual([
      "thread_started",
    ]);
    expect(slack.performed).toHaveLength(0);
  });
});
