import { afterEach, describe, expect, test } from "bun:test";
import { agent, scriptedModel, tool } from "@threads/core";
import { openStore, tenantStore } from "@threads/core/host";
import { z } from "zod";
import {
  alice,
  eventsOf,
  type Harness,
  harness,
  mailer,
  say,
  sseMessages,
  use,
} from "./kit";

// Control routes and run authority over the API: a held lease is a typed branch_busy, a parked
// child resumes its root, and the host ceiling caps every run (openapi.json, F7.6).

let h: Harness | undefined;
afterEach(async () => {
  await h?.host.stop();
  h = undefined;
});

const body = { agent: "support", input: "Hello" };
const key = { "idempotency-key": "k-1" };

describe("controls and run authority", () => {
  test("approving a parked child's call resumes its root parent to the end", async () => {
    const sent: string[] = [];
    const send = tool({
      name: "send_email",
      description: "Send an email.",
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
        responses: [use("send_email", { to: "bob" }, "m1"), say("Mailed.")],
      }),
      tools: [send],
    });
    const lead = agent({
      name: "lead",
      model: scriptedModel({
        responses: [
          use("spawn_agent", { agent: "worker", prompt: "Mail bob." }, "s1"),
          say("All done."),
        ],
      }),
      tools: [send],
      subagents: [worker],
    });
    h = harness({ agents: { lead } });
    const { call, store } = h;
    const accepted = await (
      await call("POST", "/v1/runs", {
        as: alice,
        body: { agent: "lead", input: "Go." },
        headers: key,
      })
    ).json();
    const parked = (
      await sseMessages(
        await call(
          "GET",
          `/v1/threads/${accepted.thread_id}/runs/${accepted.run_id}/events`,
          { as: alice },
        ),
      )
    ).at(-1);
    expect(parked).toMatchObject({
      result: { status: "parked", pending: [{ kind: "child" }] },
    });
    const child = z
      .object({
        result: z.object({ pending: z.array(z.object({ id: z.string() })) }),
      })
      .parse(parked).result.pending[0]?.id;
    const list = await (
      await call("GET", `/v1/threads/${child}/approvals`, { as: alice })
    ).json();
    const granted = await call(
      "POST",
      `/v1/threads/${child}/approvals/${list[0].challenge_id}`,
      { as: alice, body: { decision: "grant" } },
    );
    expect(granted.status).toBe(200);
    let types: string[] = [];
    for (let i = 0; i < 100 && !types.includes("turn_completed"); i += 1) {
      await Bun.sleep(20);
      types = (await eventsOf(store, "acme", accepted.branch_id)).map(
        (e) => e.type,
      );
    }
    expect(types).toContain("turn_completed");
    expect(sent).toEqual(["bob"]);
  });

  test("a malformed %-escape in a path parameter is 400 invalid_request", async () => {
    h = harness({ agents: { support: mailer({ responses: [] }) } });
    const r = await h.call("POST", "/v1/threads/%E0%A4%A/parked/k/resolve", {
      as: alice,
      body: { resolution: "assume_done" },
    });
    expect(r.status).toBe(400);
    expect((await r.json()).error.code).toBe("invalid_request");
  });

  test("the host ceiling caps every run it starts", async () => {
    const sent: string[] = [];
    h = harness({
      agents: {
        support: mailer({
          responses: [use("send_email", { to: "bob" }, "c1"), say("Sent.")],
          sent,
        }),
      },
      ceiling: { deny: ["send_email"] },
    });
    const { call, store } = h;
    const accepted = await (
      await call("POST", "/v1/runs", { as: alice, body, headers: key })
    ).json();
    await sseMessages(
      await call(
        "GET",
        `/v1/threads/${accepted.thread_id}/runs/${accepted.run_id}/events`,
        { as: alice },
      ),
    );
    const log = await eventsOf(store, "acme", accepted.branch_id);
    expect(
      log.find((e) => e.type === "permission_decision")?.["data"],
    ).toMatchObject({ decision: "deny", rule_id: "send_email" });
    expect(sent).toEqual([]);
  });

  test("a control route on a lease another process holds answers 409 branch_busy", async () => {
    h = harness({
      agents: {
        support: mailer({
          responses: [use("send_email", { to: "bob" }, "c1"), say("Sent.")],
        }),
      },
    });
    const { call } = h;
    const accepted = await (
      await call("POST", "/v1/runs", { as: alice, body, headers: key })
    ).json();
    await sseMessages(
      await call(
        "GET",
        `/v1/threads/${accepted.thread_id}/runs/${accepted.run_id}/events`,
        { as: alice },
      ),
    );
    const list = await (
      await call("GET", `/v1/threads/${accepted.thread_id}/approvals`, {
        as: alice,
      })
    ).json();
    const { log } = await openStore(tenantStore(h.store, alice.tenant));
    const held = log.acquire(accepted.branch_id, "another-process");
    if (!held.ok) throw new Error(held.error.message);
    try {
      const at = `/v1/threads/${accepted.thread_id}`;
      for (const [path, sent] of [
        [`${at}/approvals/${list[0].challenge_id}`, { decision: "grant" }],
        [`${at}/cancel`, undefined],
        [`${at}/mode`, { mode: "accept_edits" }],
        [`${at}/parked/k/resolve`, { resolution: "assume_done" }],
        [`${at}/questions/c9/answer`, { answer: "yes" }],
      ] as const) {
        const response = await call("POST", path, {
          as: alice,
          ...(sent === undefined ? {} : { body: sent }),
        });
        expect(response.status).toBe(409);
        expect((await response.json()).error.code).toBe("branch_busy");
      }
    } finally {
      held.value.release();
    }
  });
});
