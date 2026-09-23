import { describe, expect, test } from "bun:test";
import { z } from "zod";
import {
  agent,
  openThread,
  type RunResult,
  type Store,
  scriptedModel,
  sqlite,
  type Thread,
  tool,
} from "../../src";
import { hostRunner } from "../../src/agent/registry";
import {
  openStore,
  storeConnection,
  tenantStore,
} from "../../src/agent/sqlite";
import type { KnownEvent } from "../../src/log";
import { knownEvents } from "../../src/reduce";
import { unwrap } from "../store/helpers";

// Thread control methods (spec/api.json Thread): each appends its actor's event under its own
// lease; a parked branch then continues from the log.

const usage = { input_tokens: 10, output_tokens: 2 };
const say = (text: string) => ({
  content: [{ type: "text", text }],
  stop_reason: "end_turn",
  usage,
});
const use = (name: string, input: Record<string, unknown>, id: string) => ({
  content: [{ type: "tool_use", call_id: id, name, input }],
  stop_reason: "tool_use",
  usage,
});
const alice = { issuer: "api", tenant: "local", subject: "alice" };
const mallory = { issuer: "api", tenant: "evil", subject: "mallory" };

function mailer(sent: string[]) {
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
  return agent({
    model: scriptedModel({
      responses: [use("send_email", { to: "bob" }, "c1"), say("Done.")],
    }),
    tools: [send],
  });
}

async function parked(sent: string[]): Promise<{
  readonly store: Store;
  readonly thread: Thread;
  readonly resume: () => Promise<RunResult<unknown>>;
}> {
  const store = sqlite(":memory:");
  const bot = mailer(sent);
  const result = await bot.run("mail bob", { store });
  expect(result.status).toBe("parked");
  const thread = unwrap(await openThread(store, result.thread.id));
  const runner = hostRunner(bot);
  if (runner === undefined) throw new Error("agent() registers a host runner");
  const resume = () =>
    runner.execute({ store, principal: alice, thread: result.thread }, []);
  return { store, thread, resume };
}

async function events(
  store: Store,
  thread: Thread,
): Promise<readonly KnownEvent[]> {
  const { log } = await openStore(store);
  return knownEvents(unwrap(log.read(thread.branch)));
}

describe("approvals", () => {
  test("approve consumes the challenge once; the parked call then runs", async () => {
    const sent: string[] = [];
    const { store, thread, resume } = await parked(sent);
    const [pending] = await thread.pendingApprovals();
    if (pending === undefined) throw new Error("one open challenge");
    expect(pending).toMatchObject({ tool: "send_email", input: { to: "bob" } });
    const granted = await thread.approve(pending.challenge_id, alice);
    expect(granted.ok).toBe(true);
    const again = await thread.approve(pending.challenge_id, alice);
    expect(again).toMatchObject({
      ok: false,
      error: { code: "approval_duplicate" },
    });
    expect(await thread.pendingApprovals()).toEqual([]);
    const { db } = await storeConnection(store);
    expect(db.all("SELECT state, decided_by FROM approvals", [])).toEqual([
      { state: "granted", decided_by: "api/local/alice" },
    ]);
    const result = await resume();
    expect(result.status).toBe("completed");
    expect(sent).toEqual(["bob"]);
    const types = (await events(store, thread)).map((e) => e.type);
    expect(types).toContain("resumed");
  });

  test("deny closes the call as denied and never asks again", async () => {
    const sent: string[] = [];
    const { store, thread, resume } = await parked(sent);
    const [pending] = await thread.pendingApprovals();
    if (pending === undefined) throw new Error("one open challenge");
    unwrap(await thread.deny(pending.challenge_id, alice, { reason: "no" }));
    expect((await resume()).status).toBe("completed");
    expect(sent).toEqual([]);
    const log = await events(store, thread);
    expect(log.filter((e) => e.type === "approval_requested")).toHaveLength(1);
    const result = log.find((e) => e.type === "tool_result");
    expect(result?.type === "tool_result" && result.data.origin).toBe("denied");
  });

  test("a principal of another tenant is forbidden and appends nothing", async () => {
    const { store, thread } = await parked([]);
    const before = (await events(store, thread)).length;
    const [pending] = await thread.pendingApprovals();
    if (pending === undefined) throw new Error("one open challenge");
    const denied = await thread.approve(pending.challenge_id, mallory);
    expect(denied).toMatchObject({ ok: false, error: { code: "forbidden" } });
    expect((await events(store, thread)).length).toBe(before);
  });

  test("another tenant's view of the store does not find the thread", async () => {
    const { store, thread } = await parked([]);
    const other = await openThread(tenantStore(store, "evil"), thread.id);
    expect(other).toMatchObject({ ok: false, error: { code: "not_found" } });
    const same = await openThread(tenantStore(store, "local"), thread.id);
    expect(same.ok).toBe(true);
  });

  test("an unknown challenge is not_found; remember_rule outside the suggestions is invalid", async () => {
    const { thread } = await parked([]);
    const missing = await thread.approve(crypto.randomUUID(), alice);
    expect(missing).toMatchObject({ ok: false, error: { code: "not_found" } });
    const [pending] = await thread.pendingApprovals();
    if (pending === undefined) throw new Error("one open challenge");
    const remembered = await thread.approve(pending.challenge_id, alice, {
      rememberRule: "send_email",
    });
    expect(remembered).toMatchObject({
      ok: false,
      error: { code: "invalid_request" },
    });
  });
});

describe("cancel, mode, model, questions and parked effects", () => {
  test("cancel releases the approval wait and the run ends cancelled", async () => {
    const sent: string[] = [];
    const { thread, resume } = await parked(sent);
    unwrap(await thread.cancel(alice));
    expect((await resume()).status).toBe("cancelled");
    expect(sent).toEqual([]);
  });

  test("setMode records mode_changed; bypass without allow_bypass is invalid_transition", async () => {
    const { store, thread } = await parked([]);
    unwrap(await thread.setMode("accept_edits", alice));
    const changed = (await events(store, thread)).findLast(
      (e) => e.type === "mode_changed",
    );
    expect(changed?.type === "mode_changed" && changed.data).toEqual({
      from: "default",
      to: "accept_edits",
    });
    const bypass = await thread.setMode("bypass", alice);
    expect(bypass).toMatchObject({
      ok: false,
      error: { code: "invalid_transition" },
    });
  });

  test("setModel takes the recorded adapter; an unrecorded model is invalid_transition", async () => {
    const { store, thread } = await parked([]);
    const started = (await events(store, thread)).find(
      (e) => e.type === "thread_started",
    );
    if (started?.type !== "thread_started")
      throw new Error("no thread_started");
    const unknown = await thread.setModel(
      { model: { provider: "nobody", name: "x" } },
      alice,
    );
    expect(unknown).toMatchObject({
      ok: false,
      error: { code: "invalid_transition" },
    });
    unwrap(await thread.setModel({ model: started.data.model }, alice));
    const changed = (await events(store, thread)).findLast(
      (e) => e.type === "settings_changed",
    );
    expect(
      changed?.type === "settings_changed" && changed.data.settings.adapter,
    ).toEqual(started.data.adapter);
  });

  test("answer without an open question and resolveParked without a parked effect fail", async () => {
    const { thread } = await parked([]);
    expect(await thread.answer("c9", "yes", alice)).toMatchObject({
      ok: false,
      error: { code: "no_open_question" },
    });
    expect(
      await thread.resolveParked("nope:c1", "assume_done", alice),
    ).toMatchObject({ ok: false, error: { code: "not_parked" } });
  });

  test("branches lists the main branch as runnable", async () => {
    const { thread } = await parked([]);
    expect(await thread.branches()).toEqual([
      { branch_id: thread.branch, mode: "live", runnable: true },
    ]);
  });
});
