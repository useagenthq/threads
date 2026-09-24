import { describe, expect, test } from "bun:test";
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { z } from "zod";
import {
  agent,
  fakeSandbox,
  runEvals,
  scriptedModel,
  sqlite,
  tool,
} from "../../src";
import { openStore, storeOf } from "../../src/agent/sqlite";
import { EventId } from "../../src/log";
import { knownEvents } from "../../src/reduce";
import { openThread } from "../../src/thread";
import { caseSchema } from "../conformance/schema";
import {
  casesDir,
  lookupOrder,
  memory,
  REFUND_TURN,
  say,
  support,
  use,
} from "../evals/kit";
import { code, unwrap } from "../store/helpers";

// saveCase (spec/api.json, lane 22 A): any completed turn, the first included, with no sandbox
// snapshot; files valid against case.schema.json; the runner replays it; what can't rerun
// offline is still written, marked with why.

const MUST = { must: [{ type: "tool_call", data: { name: "lookup_order" } }] };
const json = (path: string): unknown => JSON.parse(readFileSync(path, "utf8"));

async function refundThread(bot = support(REFUND_TURN), store = memory()) {
  const run = await bot.run("Please refund order 42.", { store });
  return { run, store };
}

describe("saveCase", () => {
  test("writes every file, each valid against case.schema.json", async () => {
    const { run } = await refundThread();
    const dir = casesDir();
    const saved = unwrap(
      await run.thread.saveCase("refund", {
        expect: MUST,
        externalEffects: "stub",
        dir,
      }),
    );
    expect(readdirSync(saved.path).toSorted()).toEqual([
      "artifacts",
      "case.json",
      "expected.threads-py.json",
      "expected.threads-ts.json",
      "line0.json",
      "log.threads-py.jsonl",
      "log.threads-ts.jsonl",
      "model.json",
      "sandbox.json",
      "stubs.json",
    ]);
    const valid = (def: string, file: string) =>
      caseSchema(def).safeParse(json(join(saved.path, file))).success;
    expect(valid("Case", "case.json")).toBe(true);
    expect(valid("ModelScript", "model.json")).toBe(true);
    expect(valid("StubScript", "stubs.json")).toBe(true);
    expect(valid("SandboxScript", "sandbox.json")).toBe(true);
    expect((await runEvals({ cases: dir })).cases[0]?.status).toBe("passed");
  });

  test("a sandbox from another provider saves portable, and reruns with no sandbox", async () => {
    const fake = fakeSandbox();
    const e2b = { ...fake, info: { ...fake.info, provider: "e2b" } };
    const bot = agent({
      name: "support",
      model: scriptedModel({ responses: [say("Hello.")] }),
      sandbox: e2b,
    });
    const run = await bot.run("Hi", { store: memory() });
    const dir = casesDir();
    const saved = unwrap(
      await run.thread.saveCase("remote", {
        expect: { must: [{ type: "turn_completed" }] },
        externalEffects: "stub",
        dir,
      }),
    );
    expect(saved.portable).toBe(true);
    expect(saved.reason).toBeUndefined();
  });

  test("a turn after a snapshot records it; `at` names a user_input or that snapshot", async () => {
    const effect = tool({
      name: "deploy",
      description: "Deploy.",
      input: z.object({}),
      effect: "unguarded",
      execute: async () => "deployed",
    });
    const bot = agent({
      name: "ops",
      model: scriptedModel({
        responses: [
          use("deploy", {}, "c1"),
          say("Deployed."),
          say("Hello again."),
        ],
      }),
      tools: [effect],
      permissions: { allow: ["deploy"] },
      sandbox: fakeSandbox(),
    });
    const store = memory();
    const first = await bot.run("Deploy.", { store });
    await bot.run("Hi again.", { store, thread: first.thread });
    const { log } = await openStore(store);
    const events = knownEvents(unwrap(log.read(first.thread.branch)));
    const snapshot = events.find((e) => e.type === "snapshot");
    const inputs = events.filter((e) => e.type === "user_input");
    expect(snapshot).toBeDefined();
    const dir = casesDir();
    const byInput = unwrap(
      await first.thread.saveCase("second", {
        expect: { must: [{ type: "turn_completed" }] },
        externalEffects: "stub",
        at: EventId.parse(inputs[1]?.event_id),
        dir,
      }),
    );
    const meta = z
      .object({
        snapshot: z.object({ event_id: z.string(), provider: z.string() }),
      })
      .parse(json(join(byInput.path, "case.json")));
    expect(meta.snapshot).toEqual({
      event_id: snapshot?.event_id ?? "",
      provider: "fake",
    });
    const bySnapshot = await first.thread.saveCase("second-again", {
      expect: { must: [{ type: "turn_completed" }] },
      externalEffects: "stub",
      at: EventId.parse(snapshot?.event_id),
      dir,
    });
    expect(code(bySnapshot)).toBe("ok");
    const report = await runEvals({ cases: dir });
    expect(report.cases.map((c) => c.status)).toEqual(["passed", "passed"]);
  });

  test("an input with content parts is saved but not runnable offline", async () => {
    const bot = agent({
      name: "support",
      model: scriptedModel({ responses: [say("A cat.")] }),
    });
    const run = await bot.run([{ type: "text", text: "What is this?" }], {
      store: memory(),
    });
    const dir = casesDir();
    const saved = unwrap(
      await run.thread.saveCase("content", {
        expect: { must: [{ type: "turn_completed" }] },
        externalEffects: "stub",
        dir,
      }),
    );
    expect(saved).toMatchObject({ portable: false, reason: "content_input" });
    const report = await runEvals({ cases: dir });
    expect([report.cases[0]?.status, report.cases[0]?.reason]).toEqual([
      "skipped",
      "offline_not_runnable:content_input",
    ]);
  });

  test("a spilled result whose artifact is gone is saved but not runnable offline", async () => {
    const big = tool({
      name: "dump",
      description: "Dump the log.",
      input: z.object({}),
      effect: "read_only",
      execute: async () => "x".repeat(40_000),
    });
    const bot = agent({
      name: "support",
      model: scriptedModel({
        responses: [use("dump", {}, "c1"), say("Done.")],
      }),
      tools: [big],
      permissions: { allow: ["dump"] },
    });
    const store = memory();
    const run = await bot.run("Dump it.", { store });
    const opened = await openStore(store);
    const spilled = new TextEncoder().encode("x".repeat(40_000));
    const sha = new Bun.CryptoHasher("sha256").update(spilled).digest("hex");
    const artifacts = {
      ...opened.artifacts,
      get: (h: string) =>
        h === sha
          ? {
              ok: false as const,
              error: { code: "artifact_missing" as const, message: h },
            }
          : opened.artifacts.get(h),
    };
    const reopened = await openThread(
      storeOf({ log: opened.log, artifacts }),
      run.thread.id,
    );
    const dir = casesDir();
    const saved = unwrap(
      await unwrap(reopened).saveCase("spilled", {
        expect: { must: [{ type: "turn_completed" }] },
        externalEffects: "stub",
        dir,
      }),
    );
    expect(saved).toMatchObject({
      portable: false,
      reason: "artifact_missing",
    });
  });

  test("rubric, must and at are checked before anything is written", async () => {
    const { run } = await refundThread();
    const dir = casesDir();
    const save = (
      options: Partial<Parameters<typeof run.thread.saveCase>[1]>,
    ) =>
      run.thread.saveCase("refused", {
        expect: MUST,
        externalEffects: "stub",
        dir,
        ...options,
      });
    expect(code(await save({ rubric: [""] }))).toBe("invalid_request");
    expect(code(await save({ rubric: ["x".repeat(501)] }))).toBe(
      "invalid_request",
    );
    expect(
      code(
        await save({ rubric: Array.from({ length: 21 }, (_, i) => `c${i}`) }),
      ),
    ).toBe("invalid_request");
    expect(code(await save({ expect: { must: [] } }))).toBe("invalid_request");
    expect(
      code(await save({ expect: { must: [{ type: "compacted" }] } })),
    ).toBe("invalid_request");
    expect(
      code(
        await save({
          at: EventId.parse("0192e000-0000-7000-8000-00000000ffff"),
        }),
      ),
    ).toBe("invalid_request");
    expect(readdirSync(dir)).toEqual([]);
  });

  test("an unfinished turn is refused: the turn at <id> is not completed", async () => {
    const asking = agent({
      name: "support",
      model: scriptedModel({
        responses: [use("lookup_order", { id: "42" }, "c1")],
      }),
      tools: [lookupOrder],
      permissions: { ask: ["lookup_order"] },
    });
    const run = await asking.run("Look it up.", { store: sqlite(":memory:") });
    expect(run.status).toBe("parked");
    const { log } = await openStore(run.thread.store);
    const input = knownEvents(unwrap(log.read(run.thread.branch))).find(
      (e) => e.type === "user_input",
    );
    const saved = await run.thread.saveCase("parked", {
      expect: MUST,
      externalEffects: "stub",
      at: EventId.parse(input?.event_id),
      dir: casesDir(),
    });
    expect(saved.ok ? "" : saved.error.message).toBe(
      `the turn at ${input?.event_id} is not completed`,
    );
  });
});
