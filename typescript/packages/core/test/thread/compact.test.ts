import { describe, expect, test } from "bun:test";
import { z } from "zod";
import { agent, openThread, scriptedModel, sqlite, tool } from "../../src";
import { openStore, storeOf } from "../../src/agent/sqlite";
import { ThreadId } from "../../src/log";
import { scriptedSmall } from "../agent/kit";
import { caseStore, loadCase } from "../conformance/cases";
import {
  CHILD,
  fixture,
  ROOT,
  started,
  THREAD,
  unwrap,
} from "../store/helpers";
import {
  events,
  finished,
  KEEP,
  last,
  operator,
  requestText,
  SUMMARY,
  say,
  use,
} from "./methods-kit";

// Thread.compact (spec/api.json): an idle-only request the next run carries out first, over the
// thread as it was when asked.

const mallory = { issuer: "api", tenant: "evil", subject: "mallory" };

describe("Thread.compact", () => {
  test("records a request; the next run summarizes up to it, then answers the new input", async () => {
    const { bot, ref, thread } = await finished([
      say("Hi."),
      say(SUMMARY),
      say("Answer."),
    ]);
    const asked = unwrap(
      await thread.compact(operator, { instructions: KEEP }),
    );
    const request = last(await events(ref), "compaction_requested");
    expect(request.event_id).toBe(asked.event_id);
    expect(request.actor).toEqual({ kind: "user", principal: operator });
    expect(request.data).toEqual({ instructions: KEEP });

    const next = await bot.run("a distinct next input", {
      store: ref.store,
      thread: ref,
    });
    expect(next.status).toBe("completed");
    const log = await events(ref);
    const side = log.find(
      (e) => e.type === "model_request" && e.data.purpose === "compaction",
    );
    if (side?.type !== "model_request") throw new Error("a side request");
    expect(side.data.cause_event_id).toBe(request.event_id);
    const body = await requestText(ref.store, side);
    expect(body).not.toContain("a distinct next input");
    expect(body.trimEnd().split("\n").at(-1)).toContain(
      `Additional instructions:\\n${KEEP}`,
    );
    const compacted = last(log, "compacted");
    const input = log.find((e) => e.type === "user_input");
    expect(compacted.data).toMatchObject({
      trigger: "manual",
      cause_event_id: request.event_id,
      from_seq: input?.seq,
      to_seq: request.seq - 1,
    });
    const turn = log.findLast((e) => e.type === "model_request");
    if (turn === undefined) throw new Error("a turn request");
    const lines = (await requestText(ref.store, turn)).trimEnd().split("\n");
    expect(lines.at(-2)).toContain(SUMMARY);
    expect(lines.at(-1)).toContain("a distinct next input");
    expect(unwrap(await thread.replay())).toBeUndefined();
  });

  test("a model set after the request makes the summary: the side request is in the new epoch", async () => {
    const small = scriptedSmall([say(SUMMARY), say("Answer.")]);
    const { bot, ref, thread } = await finished([say("Hi.")], {
      fallback: [small],
    });
    unwrap(await thread.compact(operator));
    unwrap(await thread.setModel({ model: small.info.model }, operator));
    expect(
      (await bot.run("next", { store: ref.store, thread: ref })).status,
    ).toBe("completed");
    expect(small.inner.remaining()).toBe(0);
    const log = await events(ref);
    const [first, side] = log.filter((e) => e.type === "model_request");
    if (first?.type !== "model_request" || side?.type !== "model_request")
      throw new Error("two requests");
    expect(side.data.purpose).toBe("compaction");
    expect(side.data.declared_prefix).not.toEqual(first.data.declared_prefix);
    expect(unwrap(await thread.replay())).toBeUndefined();
  });

  for (const budget of [{ max_turns: 1 }, { max_wall_ms: 500 }]) {
    test(`a run refused by ${Object.keys(budget)[0]} before its ladder still answers the request`, async () => {
      const { bot, ref, thread } = await finished([say("Hi.")], { budget });
      unwrap(await thread.compact(operator));
      // The first run fits the wall budget; the pause before the next one does not.
      await Bun.sleep(600);
      const next = await bot.run("next", { store: ref.store, thread: ref });
      expect(next.status).toBe("budget_exhausted");
      const log = await events(ref);
      const tail = log.slice(
        log.findIndex((e) => e.type === "budget_exceeded"),
      );
      expect(tail.map((e) => e.type)).toEqual([
        "budget_exceeded",
        "compaction_failed",
        "turn_completed",
      ]);
      expect(tail[1]?.data).toMatchObject({
        reason: "model_error",
        cause_event_id: last(log, "compaction_requested").event_id,
      });
      expect(log.filter((e) => e.type === "model_request")).toHaveLength(1);
    });
  }

  test("a budget refusal of the summary records its failure with budget_exceeded", async () => {
    const { bot, ref, thread } = await finished([say("Hi.")], {
      budget: { max_model_requests: 1 },
    });
    unwrap(await thread.compact(operator));
    const next = await bot.run("next", { store: ref.store, thread: ref });
    expect(next.status).toBe("budget_exhausted");
    const log = await events(ref);
    const tail = log.slice(log.findIndex((e) => e.type === "budget_exceeded"));
    expect(tail.map((e) => e.type)).toEqual([
      "budget_exceeded",
      "compaction_failed",
      "turn_completed",
    ]);
    expect(tail[1]).toMatchObject({
      data: {
        reason: "model_error",
        cause_event_id: last(log, "compaction_requested").event_id,
      },
    });
    expect(log.filter((e) => e.type === "model_request")).toHaveLength(1);
  });

  test("empty instructions are invalid_request, and nothing is appended", async () => {
    const { ref, thread } = await finished([say("Hi.")]);
    const before = (await events(ref)).length;
    expect(await thread.compact(operator, { instructions: "" })).toMatchObject({
      ok: false,
      error: { code: "invalid_request" },
    });
    expect((await events(ref)).length).toBe(before);
  });

  test("a second request before the run is invalid_transition; after the outcome one is accepted", async () => {
    const { bot, ref, thread } = await finished([
      say("Hi."),
      say(SUMMARY),
      say("Answer."),
    ]);
    unwrap(await thread.compact(operator));
    expect(await thread.compact(operator)).toMatchObject({
      ok: false,
      error: {
        code: "invalid_transition",
        message: "a compaction is already requested",
      },
    });
    await bot.run("next", { store: ref.store, thread: ref });
    expect((await thread.compact(operator)).ok).toBe(true);
  });

  test("nothing to compact yet is invalid_transition; another tenant is forbidden", async () => {
    const f = fixture();
    unwrap(f.store.createBranch(THREAD, ROOT));
    const writer = unwrap(f.store.acquire(ROOT, "setup"));
    unwrap(writer.append([started]));
    writer.release();
    const store = storeOf({ log: f.store, artifacts: f.artifacts });
    const thread = unwrap(await openThread(store, THREAD));
    expect(await thread.compact(operator)).toMatchObject({
      ok: false,
      error: { code: "invalid_transition", message: "nothing to compact yet" },
    });
    expect(await thread.compact(mallory)).toMatchObject({
      ok: false,
      error: { code: "forbidden" },
    });
  });

  test("an inspection-only branch is branch_not_runnable for both idle controls", async () => {
    const c = loadCase("repair-child-inspection-only");
    const f = caseStore(c);
    unwrap(f.store.importLog(c.log ?? new Uint8Array()));
    const store = storeOf({ log: f.store, artifacts: f.artifacts });
    const thread = unwrap(await openThread(store, THREAD, { branchId: CHILD }));
    for (const done of [
      await thread.compact(operator),
      await thread.setOutputStyle("concise", operator),
    ])
      expect(done).toMatchObject({
        ok: false,
        error: { code: "branch_not_runnable" },
      });
  });
});

describe("idle only", () => {
  test("during this process's own run: branch_busy, nothing appended", async () => {
    const store = sqlite(":memory:");
    const seen: string[] = [];
    const probe = tool({
      name: "probe",
      description: "Asks for a compaction mid-turn.",
      input: z.object({}),
      runs: "host",
      effect: "read_only",
      execute: async (_input, ctx) => {
        const thread = unwrap(
          await openThread(store, ThreadId.parse(ctx.threadId)),
        );
        for (const done of [
          await thread.compact(operator),
          await thread.setOutputStyle("concise", operator),
        ])
          seen.push(done.ok ? "ok" : done.error.code);
        return "done";
      },
    });
    const bot = agent({
      model: scriptedModel({ responses: [use("probe", {}, "c1"), say("x")] }),
      tools: [probe],
      outputStyles: { concise: "Be brief." },
    });
    const result = await bot.run("go", { store });
    expect(seen).toEqual(["branch_busy", "branch_busy"]);
    const log = await events(result.thread);
    expect(log.some((e) => e.type === "compaction_requested")).toBe(false);
    expect(log.some((e) => e.type === "injected")).toBe(false);
  });

  test("a lease another process holds, and a turn parked on an approval: branch_busy", async () => {
    const send = tool({
      name: "send_email",
      description: "Send an email.",
      input: z.object({ to: z.string() }),
      runs: "host",
      execute: async () => "sent",
    });
    const store = sqlite(":memory:");
    const bot = agent({
      model: scriptedModel({
        responses: [use("send_email", { to: "bob" }, "c1")],
      }),
      tools: [send],
      outputStyles: { concise: "Be brief." },
    });
    const result = await bot.run("mail bob", { store });
    expect(result.status).toBe("parked");
    const thread = unwrap(await openThread(store, result.thread.id));
    const before = (await events(result.thread)).length;
    const busy = { ok: false, error: { code: "branch_busy" } };
    expect(await thread.compact(operator)).toMatchObject(busy);
    expect(await thread.setOutputStyle("concise", operator)).toMatchObject(
      busy,
    );
    const { log } = await openStore(store);
    const other = unwrap(log.acquire(thread.branch, "another-process"));
    try {
      expect(await thread.compact(operator)).toMatchObject(busy);
    } finally {
      other.release();
    }
    expect((await events(result.thread)).length).toBe(before);
  });
});
