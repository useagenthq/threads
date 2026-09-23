import { afterEach, describe, expect, test } from "bun:test";
import { storeConnection } from "@threads/core/host";
import {
  alice,
  bob,
  eve,
  eventsOf,
  type Harness,
  harness,
  mailer,
  say,
  sseMessages,
  use,
} from "./kit";

// POST /v1/runs and its events stream (openapi.json startRun, subscribeRun; F12.11, F12.12).

let h: Harness | undefined;
afterEach(async () => {
  await h?.host.stop();
  h = undefined;
});

function start(options: { readonly responses: readonly unknown[] }): Harness {
  h = harness({ agents: { support: mailer(options) } });
  return h;
}

const body = { agent: "support", input: "Hello" };
const key = { "idempotency-key": "k-1" };

describe("authentication", () => {
  test("without authenticate every /v1 route is 401", async () => {
    const { host } = await import("../src");
    const { sqlite } = await import("@threads/core");
    const bare = host({ store: sqlite(":memory:"), agents: {} });
    const response = await bare.fetch(
      new Request("http://x/v1/runs", { method: "POST" }),
    );
    expect(response.status).toBe(401);
    expect(await response.json()).toEqual({
      error: { code: "unauthenticated", message: expect.any(String) },
    });
  });

  test("an unauthenticated request is 401 and appends nothing", async () => {
    const { call } = start({ responses: [say("Hi")] });
    const response = await call("POST", "/v1/runs", { body, headers: key });
    expect(response.status).toBe(401);
  });
});

describe("POST /v1/runs", () => {
  test("202 with run_id = the user_input's event_id; the stream ends with its result", async () => {
    const { call, store } = start({ responses: [say("Hi there")] });
    const response = await call("POST", "/v1/runs", {
      as: alice,
      body,
      headers: key,
    });
    expect(response.status).toBe(202);
    const accepted = await response.json();
    const log = await eventsOf(store, "acme", accepted.branch_id);
    const input = log.find((e) => e.type === "user_input");
    expect(input?.["event_id"]).toBe(accepted.run_id);
    expect(input?.["actor"]).toEqual({ kind: "user", principal: alice });
    const stream = await call(
      "GET",
      `/v1/threads/${accepted.thread_id}/runs/${accepted.run_id}/events`,
      { as: alice },
    );
    expect(stream.headers.get("content-type")).toBe("text/event-stream");
    const messages = await sseMessages(stream);
    expect(messages.at(-1)).toEqual({
      kind: "result",
      run_id: accepted.run_id,
      result: {
        status: "completed",
        thread_id: accepted.thread_id,
        branch_id: accepted.branch_id,
        output: "Hi there",
      },
    });
    expect(messages[0]).toMatchObject({
      kind: "event",
      event: { type: "user_input" },
    });
  });

  test("no Idempotency-Key is invalid_request", async () => {
    const { call } = start({ responses: [say("Hi")] });
    const response = await call("POST", "/v1/runs", { as: alice, body });
    expect(response.status).toBe(400);
    expect((await response.json()).error.code).toBe("invalid_request");
  });

  test("lost response: the same key and body replay the receipt and start nothing (F12.11)", async () => {
    const { call, store } = start({ responses: [say("Hi")] });
    const first = await (
      await call("POST", "/v1/runs", { as: alice, body, headers: key })
    ).json();
    const again = await call("POST", "/v1/runs", {
      as: alice,
      body,
      headers: key,
    });
    expect(again.status).toBe(202);
    expect(await again.json()).toEqual(first);
    const log = await eventsOf(store, "acme", first.branch_id);
    expect(log.filter((e) => e.type === "user_input")).toHaveLength(1);
    const other = await call("POST", "/v1/runs", {
      as: alice,
      body: { ...body, input: "Something else" },
      headers: key,
    });
    expect(other.status).toBe(409);
    expect((await other.json()).error.code).toBe("idempotency_key_reused");
  });

  test("another principal of the tenant with the same key is refused and learns nothing (F12.12)", async () => {
    const { call, store } = start({ responses: [say("Hi")] });
    const first = await (
      await call("POST", "/v1/runs", { as: alice, body, headers: key })
    ).json();
    const response = await call("POST", "/v1/runs", {
      as: bob,
      body,
      headers: key,
    });
    expect(response.status).toBe(409);
    const text = await response.text();
    expect(JSON.parse(text).error.code).toBe(
      "idempotency_key_principal_mismatch",
    );
    expect(text).not.toContain(first.run_id);
    expect(text).not.toContain(first.thread_id);
    const log = await eventsOf(store, "acme", first.branch_id);
    expect(log.filter((e) => e.type === "user_input")).toHaveLength(1);
  });

  test("the same key in another tenant is its own key", async () => {
    const { call } = start({ responses: [say("Hi"), say("Hi")] });
    const a = await call("POST", "/v1/runs", { as: alice, body, headers: key });
    const e = await call("POST", "/v1/runs", { as: eve, body, headers: key });
    expect(a.status).toBe(202);
    expect(e.status).toBe(202);
  });

  test("another tenant's thread is not_found; an unknown agent is not_found", async () => {
    const { call } = start({ responses: [say("Hi")] });
    const first = await (
      await call("POST", "/v1/runs", { as: alice, body, headers: key })
    ).json();
    const stolen = await call("POST", "/v1/runs", {
      as: eve,
      body: { ...body, thread_id: first.thread_id },
      headers: { "idempotency-key": "k-2" },
    });
    expect(stolen.status).toBe(404);
    const timeline = await call(
      "GET",
      `/v1/threads/${first.thread_id}/timeline`,
      { as: eve },
    );
    expect(timeline.status).toBe(404);
    const nobody = await call("POST", "/v1/runs", {
      as: alice,
      body: { ...body, agent: "nobody" },
      headers: { "idempotency-key": "k-3" },
    });
    expect(nobody.status).toBe(404);
  });

  test("a log from a newer writer answers unsupported_critical_event, not not_found", async () => {
    const { call, store } = start({ responses: [say("Hi")] });
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
    const { db } = await storeConnection(store);
    db.run(
      `UPDATE events SET line = CAST(replace(CAST(line AS TEXT), '"type":"turn_completed"', '"type":"approval_quorum"') AS BLOB) WHERE branch_id = ?`,
      [accepted.branch_id],
    );
    const response = await call(
      "GET",
      `/v1/threads/${accepted.thread_id}/timeline`,
      { as: alice },
    );
    expect(response.status).toBe(409);
    expect((await response.json()).error.code).toBe(
      "unsupported_critical_event",
    );
  });

  test("a branch_id that is not the thread's is not_found", async () => {
    const { call } = start({ responses: [say("Hi")] });
    const first = await (
      await call("POST", "/v1/runs", { as: alice, body, headers: key })
    ).json();
    const response = await call(
      "GET",
      `/v1/threads/${first.thread_id}/timeline?branch_id=${crypto.randomUUID()}`,
      { as: alice },
    );
    expect(response.status).toBe(404);
  });
});

describe("approvals over the API", () => {
  test("a parked run continues once an approval is granted; a second grant is approval_duplicate", async () => {
    const sent: string[] = [];
    h = harness({
      agents: {
        support: mailer({
          responses: [use("send_email", { to: "bob" }, "c1"), say("Sent.")],
          sent,
        }),
      },
    });
    const { call } = h;
    const accepted = await (
      await call("POST", "/v1/runs", { as: alice, body, headers: key })
    ).json();
    const events = `/v1/threads/${accepted.thread_id}/runs/${accepted.run_id}/events`;
    const parked = (
      await sseMessages(await call("GET", events, { as: alice }))
    ).at(-1);
    expect(parked).toMatchObject({
      result: { status: "parked", reason: "awaiting_approval" },
    });
    const busy = await call("POST", "/v1/runs", {
      as: alice,
      body: { ...body, thread_id: accepted.thread_id },
      headers: { "idempotency-key": "k-2" },
    });
    expect((await busy.json()).error.code).toBe("branch_busy");
    const list = await (
      await call("GET", `/v1/threads/${accepted.thread_id}/approvals`, {
        as: alice,
      })
    ).json();
    expect(list).toHaveLength(1);
    const path = `/v1/threads/${accepted.thread_id}/approvals/${list[0].challenge_id}`;
    const granted = await call("POST", path, {
      as: alice,
      body: { decision: "grant" },
    });
    expect(granted.status).toBe(200);
    expect(Object.keys(await granted.json())).toEqual(["event_id"]);
    const twice = await call("POST", path, {
      as: alice,
      body: { decision: "grant" },
    });
    expect(twice.status).toBe(409);
    expect((await twice.json()).error.code).toBe("approval_duplicate");
    const timeline = `/v1/threads/${accepted.thread_id}/timeline`;
    let types: string[] = [];
    for (let i = 0; i < 100 && !types.includes("turn_completed"); i += 1) {
      await Bun.sleep(20);
      const t = await (await call("GET", timeline, { as: alice })).json();
      types = t.entries.map((e: { event: { type: string } }) => e.event.type);
    }
    expect(types).toContain("turn_completed");
    expect(sent).toEqual(["bob"]);
  });

  test("with approvers configured, anyone else is forbidden", async () => {
    h = harness({
      agents: {
        support: mailer({
          responses: [use("send_email", { to: "bob" }, "c1"), say("Sent.")],
          approvers: [alice],
        }),
      },
    });
    const { call } = h;
    const accepted = await (
      await call("POST", "/v1/runs", { as: bob, body, headers: key })
    ).json();
    await sseMessages(
      await call(
        "GET",
        `/v1/threads/${accepted.thread_id}/runs/${accepted.run_id}/events`,
        {
          as: bob,
        },
      ),
    );
    const list = await (
      await call("GET", `/v1/threads/${accepted.thread_id}/approvals`, {
        as: bob,
      })
    ).json();
    const path = `/v1/threads/${accepted.thread_id}/approvals/${list[0].challenge_id}`;
    const response = await call("POST", path, {
      as: bob,
      body: { decision: "grant" },
    });
    expect(response.status).toBe(403);
    expect((await response.json()).error.code).toBe("forbidden");
  });
});
