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
import { type KnownEvent, ThreadId } from "../../src/log";
import { knownEvents } from "../../src/reduce";
import { keepLease } from "../../src/store";
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

function mailer(sent: string[], more: readonly unknown[] = []) {
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
      responses: [
        use("send_email", { to: "bob" }, "c1"),
        ...more,
        say("Done."),
      ],
    }),
    tools: [send],
  });
}

async function parked(
  sent: string[],
  more: readonly unknown[] = [],
): Promise<{
  readonly store: Store;
  readonly thread: Thread;
  readonly resume: () => Promise<RunResult<unknown>>;
}> {
  const store = sqlite(":memory:");
  const bot = mailer(sent, more);
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
  return knownEvents(unwrap(await log.read(thread.branch)));
}

describe("controls through a live run's writer", () => {
  test("two controls at once each plan against the chain the other left: both mode changes land", async () => {
    const { store, thread } = await parked([]);
    const { log } = await openStore(store);
    // This process runs the branch: controls go through its writer, queued on its lock.
    const writer = unwrap(await log.acquire(thread.branch, "live-run"));
    const stop = keepLease(writer);
    try {
      const [first, second] = await Promise.all([
        thread.setMode("plan", alice),
        thread.setMode("accept_edits", alice),
      ]);
      expect(first.ok).toBe(true);
      expect(second.ok).toBe(true);
    } finally {
      await stop();
    }
    const changes = (await events(store, thread)).flatMap((e) =>
      e.type === "mode_changed" ? [[e.data.from, e.data.to]] : [],
    );
    expect(changes).toEqual([
      ["default", "plan"],
      ["plan", "accept_edits"],
    ]);
  });
});

describe("approvals", () => {
  test("approve consumes the challenge once; the parked call then runs", async () => {
    const sent: string[] = [];
    const { store, thread, resume } = await parked(sent);
    const [pending] = unwrap(await thread.pendingApprovals());
    if (pending === undefined) throw new Error("one open challenge");
    expect(pending).toMatchObject({ tool: "send_email", input: { to: "bob" } });
    const granted = await thread.approve(pending.challenge_id, alice);
    expect(granted.ok).toBe(true);
    const again = await thread.approve(pending.challenge_id, alice);
    expect(again).toMatchObject({
      ok: false,
      error: { code: "approval_duplicate" },
    });
    expect(unwrap(await thread.pendingApprovals())).toEqual([]);
    const { db } = await storeConnection(store);
    expect(
      await db.transaction((tx) =>
        tx.all("SELECT state, decided_by FROM approvals", []),
      ),
    ).toEqual([{ state: "granted", decided_by: "api/local/alice" }]);
    const result = await resume();
    expect(result.status).toBe("completed");
    expect(sent).toEqual(["bob"]);
    const types = (await events(store, thread)).map((e) => e.type);
    expect(types).toContain("resumed");
  });

  test("deny closes the call as denied and never asks again", async () => {
    const sent: string[] = [];
    const { store, thread, resume } = await parked(sent);
    const [pending] = unwrap(await thread.pendingApprovals());
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
    const [pending] = unwrap(await thread.pendingApprovals());
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
    const [pending] = unwrap(await thread.pendingApprovals());
    if (pending === undefined) throw new Error("one open challenge");
    expect(pending.suggested_rules).toEqual(["send_email"]);
    const remembered = await thread.approve(pending.challenge_id, alice, {
      rememberRule: "bash(rm -rf /)",
    });
    expect(remembered).toMatchObject({
      ok: false,
      error: { code: "invalid_request" },
    });
  });

  test("a suggested remember_rule is kept as permission_rule_added", async () => {
    const { store, thread } = await parked([]);
    const [pending] = unwrap(await thread.pendingApprovals());
    if (pending === undefined) throw new Error("one open challenge");
    unwrap(
      await thread.approve(pending.challenge_id, alice, {
        rememberRule: "send_email",
      }),
    );
    const added = (await events(store, thread)).find(
      (e) => e.type === "permission_rule_added",
    );
    expect(added).toMatchObject({
      data: {
        rule: "send_email",
        decision: "allow",
        challenge_id: pending.challenge_id,
      },
    });
  });

  test("a remembered rule allows the next matching call on the thread", async () => {
    const sent: string[] = [];
    const next = use("send_email", { to: "carol" }, "c2");
    const { store, thread, resume } = await parked(sent, [next]);
    const [pending] = unwrap(await thread.pendingApprovals());
    if (pending === undefined) throw new Error("one open challenge");
    unwrap(
      await thread.approve(pending.challenge_id, alice, {
        rememberRule: "send_email",
      }),
    );
    expect((await resume()).status).toBe("completed");
    const decided = (await events(store, thread)).flatMap((e) =>
      e.type === "permission_decision"
        ? [[e.data.decision, e.data.source, e.data.rule_id]]
        : [],
    );
    expect(decided).toEqual([
      ["ask", "mode", undefined],
      ["allow", "thread_rule", "send_email"],
    ]);
    expect(sent).toEqual(["bob", "carol"]);
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

  test("a lease another holder has is branch_busy, typed, and appends nothing", async () => {
    const { store, thread } = await parked([]);
    const { log } = await openStore(store);
    const other = unwrap(await log.acquire(thread.branch, "another-process"));
    try {
      const before = (await events(store, thread)).length;
      const [pending] = unwrap(await thread.pendingApprovals());
      if (pending === undefined) throw new Error("one open challenge");
      for (const done of [
        await thread.cancel(alice),
        await thread.approve(pending.challenge_id, alice),
        await thread.setMode("accept_edits", alice),
        await thread.resolveParked("k", "assume_done", alice),
      ])
        expect(done).toMatchObject({
          ok: false,
          error: { code: "branch_busy" },
        });
      expect((await events(store, thread)).length).toBe(before);
    } finally {
      await other.release();
    }
  });

  test("a control during this process's own run appends through the run's writer", async () => {
    const store = sqlite(":memory:");
    const { promise: gate, resolve: open } = Promise.withResolvers<void>();
    const slow = tool({
      name: "slow",
      description: "Waits.",
      input: z.object({}),
      runs: "host",
      effect: "read_only",
      execute: async () => {
        await gate;
        return "ok";
      },
    });
    const bot = agent({
      model: scriptedModel({ responses: [use("slow", {}, "c1"), say("x")] }),
      tools: [slow],
    });
    const running = bot.run("go", { store });
    let thread: Thread | undefined;
    for (let i = 0; i < 100 && thread === undefined; i++) {
      await Bun.sleep(5);
      const { db } = await storeConnection(store);
      const [row] = z
        .array(z.object({ thread_id: ThreadId }))
        .parse(
          await db.transaction((tx) =>
            tx.all("SELECT thread_id FROM branches", []),
          ),
        );
      if (row !== undefined) {
        const opened = await openThread(store, row.thread_id);
        if (opened.ok) thread = opened.value;
      }
    }
    if (thread === undefined) throw new Error("the run started a thread");
    const cancelled = await thread.cancel(alice);
    open();
    expect(cancelled.ok).toBe(true);
    expect((await running).status).toBe("cancelled");
  });

  test("branches lists the main branch as runnable", async () => {
    const { thread } = await parked([]);
    expect(await thread.branches()).toEqual([
      { branch_id: thread.branch, mode: "live", runnable: true },
    ]);
  });
});
