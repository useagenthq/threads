import { afterEach, describe, expect, test } from "bun:test";
import { agent, scriptedModel, tool } from "@threads/core";
import { z } from "zod";
import {
  alice,
  bob,
  eventsOf,
  type Harness,
  harness,
  mailer,
  say,
  sseMessages,
  use,
} from "./kit";

// Approval authority over the host API (spec/schema/README.md, "Approval authority"): the root
// run's approver set, else only its originating principal; a descendant's route never widens it.

let h: Harness | undefined;
afterEach(async () => {
  await h?.host.stop();
  h = undefined;
});

const Parked = z.object({
  result: z.object({ pending: z.array(z.object({ id: z.string() })) }),
});

async function started(harnessed: Harness, agentKey: string) {
  const accepted = await (
    await harnessed.call("POST", "/v1/runs", {
      as: alice,
      body: { agent: agentKey, input: "Go." },
      headers: { "idempotency-key": "k-1" },
    })
  ).json();
  const last = (
    await sseMessages(
      await harnessed.call(
        "GET",
        `/v1/threads/${accepted.thread_id}/runs/${accepted.run_id}/events`,
        { as: alice },
      ),
    )
  ).at(-1);
  return { accepted, last };
}

async function challengeOf(harnessed: Harness, threadId: string) {
  const list = await (
    await harnessed.call("GET", `/v1/threads/${threadId}/approvals`, {
      as: alice,
    })
  ).json();
  return z.array(z.object({ challenge_id: z.string() })).parse(list)[0]
    ?.challenge_id;
}

describe("approval authority", () => {
  test("unconfigured: another tenant principal is forbidden, the originating principal approves", async () => {
    const sent: string[] = [];
    h = harness({
      agents: {
        support: mailer({
          responses: [use("send_email", { to: "bob" }, "c1"), say("Sent.")],
          sent,
        }),
      },
    });
    const { accepted } = await started(h, "support");
    const challenge = await challengeOf(h, accepted.thread_id);
    const at = `/v1/threads/${accepted.thread_id}/approvals/${challenge}`;
    const byBob = await h.call("POST", at, {
      as: bob,
      body: { decision: "grant" },
    });
    expect(byBob.status).toBe(403);
    const byAlice = await h.call("POST", at, {
      as: alice,
      body: { decision: "grant" },
    });
    expect(byAlice.status).toBe(200);
  });

  test("a subagent's challenge through the child route uses the root's approvers", async () => {
    const sent: string[] = [];
    const send = tool({
      name: "send_email",
      description: "Send.",
      input: z.object({ to: z.string() }),
      runs: "host",
      execute: async ({ to }) => {
        sent.push(to);
        return "sent";
      },
    });
    const worker = agent({
      name: "worker",
      model: scriptedModel({
        responses: [use("send_email", { to: "ceo" }, "m1"), say("ok")],
      }),
      tools: [send],
    });
    const lead = agent({
      name: "lead",
      model: scriptedModel({
        responses: [
          use("spawn_agent", { agent: "worker", prompt: "x" }, "s1"),
          say("done"),
        ],
      }),
      tools: [send],
      subagents: [worker],
      approvers: [alice],
    });
    h = harness({ agents: { lead } });
    const { last } = await started(h, "lead");
    const child = Parked.parse(last).result.pending[0]?.id ?? "";
    const challenge = await challengeOf(h, child);
    const r = await h.call(
      "POST",
      `/v1/threads/${child}/approvals/${challenge}`,
      { as: bob, body: { decision: "grant" } },
    );
    expect(r.status).toBe(403);
    await Bun.sleep(50);
    expect(sent).toEqual([]);
  });

  test("resolveParked needs approval authority and appends nothing without it", async () => {
    h = harness({
      agents: {
        support: mailer({
          responses: [use("send_email", { to: "bob" }, "c1"), say("Sent.")],
          approvers: [alice],
        }),
      },
    });
    const { accepted } = await started(h, "support");
    const before = await eventsOf(h.store, "acme", accepted.branch_id);
    const r = await h.call(
      "POST",
      `/v1/threads/${accepted.thread_id}/parked/${accepted.branch_id}:c1/resolve`,
      { as: bob, body: { resolution: "assume_not_done" } },
    );
    expect(r.status).toBe(403);
    expect((await r.json()).error.code).toBe("forbidden");
    expect(await eventsOf(h.store, "acme", accepted.branch_id)).toHaveLength(
      before.length,
    );
  });
});
