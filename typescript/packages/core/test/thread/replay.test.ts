import { describe, expect, test } from "bun:test";
import { mkdtempSync, rmSync, unlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { agent, openThread, scriptedModel, sqlite } from "../../src";
import { openStore, storeOf } from "../../src/agent/sqlite";
import { sha256Hex } from "../../src/hash";
import { err } from "../../src/result";
import { logError } from "../../src/verify";
import { caseStore, loadCase } from "../conformance/cases";
import {
  CHILD,
  fixture,
  ROOT,
  started,
  THREAD,
  unwrap,
  userInput,
} from "../store/helpers";
import {
  append,
  events,
  finished,
  last,
  operator,
  SUMMARY,
  say,
  use,
} from "./methods-kit";
import { corrupt } from "./usage-kit";

// Thread.replay (spec/api.json): every recorded request re-renders from the log, with no model
// call and no append; the first failure names the request's seq.

const ok = { ok: true, value: undefined } as const;

/** Two turns with a tool call, an output style and a requested compaction between them. */
async function rich() {
  const t = await finished(
    [
      use("todo_write", { todos: [] }, "c1"),
      say("Hi."),
      say(SUMMARY),
      say("Styled."),
    ],
    { outputStyles: { concise: "Be brief." } },
  );
  unwrap(await t.thread.setOutputStyle("concise", operator));
  unwrap(await t.thread.compact(operator));
  expect(
    (await t.bot.run("again", { store: t.store, thread: t.ref })).status,
  ).toBe("completed");
  return t;
}

describe("Thread.replay", () => {
  test("a recorded thread reproduces: Ok, no model request, nothing appended", async () => {
    const { ref, thread } = await rich();
    const before = await events(ref);
    for (const type of ["tool_call", "compacted", "injected"])
      expect(before.some((e) => e.type === type)).toBe(true);
    // The handle holds no model: replay re-renders, it never sends.
    expect(await thread.replay()).toEqual(ok);
    expect((await events(ref)).length).toBe(before.length);
  });

  test("a branch with no model request replays trivially", async () => {
    const f = await fixture();
    unwrap(await f.store.createBranch(THREAD, ROOT));
    const writer = unwrap(await f.store.acquire(ROOT, "setup"));
    unwrap(await writer.append([started]));
    await writer.release();
    const store = storeOf({ log: f.store, artifacts: f.artifacts });
    expect(await unwrap(await openThread(store, THREAD)).replay()).toEqual(ok);
  });

  test("a request whose bytes no longer render the same is request_hash_mismatch at its seq", async () => {
    const { ref, thread } = await finished([say("Hi."), say("Again.")]);
    const { artifacts } = await openStore(ref.store);
    const request = last(await events(ref), "model_request");
    const other = new TextEncoder().encode("not what Render v1 makes\n");
    await append(ref, userInput("next"), {
      ...draftOf(request),
      data: {
        ...request.data,
        request_ref: {
          sha256: await artifacts.put(other),
          bytes: other.length,
          media_type: "application/x-ndjson",
        },
      },
    });
    const seq = last(await events(ref), "model_request").seq;
    expect(await thread.replay()).toMatchObject({
      ok: false,
      error: { code: "request_hash_mismatch", seq },
    });
  });

  test("a declared prefix other than its epoch's line 0 is prefix_changed at its seq", async () => {
    const { ref, thread } = await finished([say("Hi.")]);
    const request = last(await events(ref), "model_request");
    const line = new TextEncoder().encode("{}\n");
    await append(ref, userInput("next"), {
      ...draftOf(request),
      data: {
        ...request.data,
        declared_prefix: { bytes: line.length, sha256: sha256Hex(line) },
      },
    });
    const seq = last(await events(ref), "model_request").seq;
    expect(await thread.replay()).toMatchObject({
      ok: false,
      error: { code: "prefix_changed", seq },
    });
  });

  test("a stored request changed or removed after the fact: artifact_corrupt, artifact_missing", async () => {
    const dir = mkdtempSync(join(tmpdir(), "threads-replay-"));
    try {
      const store = sqlite(dir);
      const bot = agent({ model: scriptedModel({ responses: [say("Hi.")] }) });
      const first = await bot.run("hello", { store });
      const thread = unwrap(await openThread(store, first.thread.id));
      const request = last(await events(first.thread), "model_request");
      const sha = request.data.request_ref.sha256;
      const file = join(dir, "artifacts", "sha256", sha.slice(0, 2), sha);
      writeFileSync(file, "x".repeat(request.data.request_ref.bytes));
      expect(await thread.replay()).toMatchObject({
        ok: false,
        error: { code: "artifact_corrupt", seq: request.seq },
      });
      unlinkSync(file);
      expect(await thread.replay()).toMatchObject({
        ok: false,
        error: { code: "artifact_missing", seq: request.seq },
      });
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  test("a fork covers its parent's prefix: a parent request gone fails the child at the parent's seq", async () => {
    const c = loadCase("repair-child-inspection-only");
    const f = await caseStore(c);
    unwrap(await f.store.importLog(c.log ?? new Uint8Array()));
    const first = f.store.read(CHILD);
    const request = unwrap(await first).events.find(
      (e) => e.kind === "event" && e.event.type === "model_request",
    );
    if (request?.kind !== "event" || request.event.type !== "model_request")
      throw new Error("the parent made a request");
    const gone = request.event.data.request_ref.sha256;
    const artifacts = {
      ...f.artifacts,
      get: async (sha: string) =>
        sha === gone
          ? err(logError("artifact_missing", `artifact ${sha} is missing`))
          : f.artifacts.get(sha),
    };
    const store = storeOf({ log: f.store, artifacts });
    const child = unwrap(await openThread(store, THREAD, { branchId: CHILD }));
    expect(await child.replay()).toMatchObject({
      ok: false,
      error: { code: "artifact_missing", seq: request.event.seq },
    });
  });

  test("an unreadable log is log_corrupt, not a replay verdict", async () => {
    const { ref, thread } = await finished([say("Hi.")]);
    await corrupt(ref.store, ref.id, "hello");
    expect(await thread.replay()).toMatchObject({
      ok: false,
      error: { code: "log_corrupt" },
    });
  });
});

/** A request event as a draft, to append a changed copy of it. */
function draftOf(
  e: Extract<
    Awaited<ReturnType<typeof events>>[number],
    { type: "model_request" }
  >,
) {
  const { type, type_version, critical, actor } = e;
  return { type, type_version, critical, actor };
}
