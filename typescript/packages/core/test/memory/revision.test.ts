import { describe, expect, test } from "bun:test";
import {
  agent,
  fakeSandbox,
  type KnowledgeProvider,
  type RunResult,
  scriptedModel,
  sqlite,
} from "../../src";
import { openStore } from "../../src/agent/sqlite";
import { knownEvents, reduce } from "../../src/reduce";
import { err, ok } from "../../src/result";
import { unwrap } from "../store/helpers";

// KnowledgeProvider.revision is required, and a revision that fails costs that turn end its
// fork point: a snapshot on a knowledge-bound thread always records knowledge_revision, so a
// pinned fork always has something to pin to.

const usage = { input_tokens: 10, output_tokens: 2 };
const readOnly = {
  code: "invalid",
  message: "this corpus is read-only",
} as const;

/** An empty, read-only corpus whose revision fails once `fail()` is called. */
function corpus(): {
  readonly provider: KnowledgeProvider;
  readonly fail: () => void;
  readonly reads: () => number;
} {
  let failing = false;
  let reads = 0;
  const provider: KnowledgeProvider = {
    ingest: async () => err(readOnly),
    remove: async () => err(readOnly),
    search: async () => ok([]),
    get: async () => err(readOnly),
    revision: async () => {
      reads += 1;
      return failing
        ? err({ code: "unavailable", message: "the index is down" } as const)
        : ok(7);
    },
  };
  return {
    provider,
    reads: () => reads,
    fail: () => {
      failing = true;
    },
  };
}

const write = (path: string, id: string) => ({
  content: [
    {
      type: "tool_use",
      call_id: id,
      name: "write",
      input: { path, content: path },
    },
  ],
  stop_reason: "tool_use",
  usage,
});
const done = {
  content: [{ type: "text", text: "Done." }],
  stop_reason: "end_turn",
  usage,
};

async function logOf<T>(result: RunResult<T>) {
  const { log } = await openStore(result.thread.store);
  const chain = unwrap(await log.read(result.thread.branch));
  return {
    snapshots: knownEvents(chain).flatMap((e) =>
      e.type === "snapshot" ? [e.data.knowledge_revision] : [],
    ),
    forkPoints: reduce(chain, Date.now()).fork_points.length,
    rows: unwrap(await log.ledger.rows()).length,
  };
}

describe("KnowledgeProvider.revision", () => {
  test("is required", () => {
    // @ts-expect-error revision is required
    const provider: KnowledgeProvider = {
      ingest: async () => err(readOnly),
      remove: async () => err(readOnly),
      search: async () => ok([]),
      get: async () => err(readOnly),
    };
    expect("revision" in provider).toBe(false);
  });

  test("an error skips that turn end's snapshot, and no scratch sandbox is made for it", async () => {
    const kb = corpus();
    const bot = agent({
      model: scriptedModel({
        responses: [write("a.txt", "c1"), done, write("b.txt", "c2"), done],
      }),
      sandbox: fakeSandbox(),
      knowledge: kb.provider,
      permissions: { mode: "accept_edits" },
    });
    const first = await bot.run("one", { store: sqlite(":memory:") });
    expect(first.status).toBe("completed");
    const before = await logOf(first);
    kb.fail();
    const second = await bot.run("two", { thread: first.thread });
    expect(second.status).toBe("completed");
    const after = await logOf(second);
    expect(after.snapshots).toEqual([7]);
    expect(after.forkPoints).toBe(1);
    expect(after.rows).toBe(before.rows);
  });

  test("is read only when a capture follows", async () => {
    const kb = corpus();
    const bot = agent({
      model: scriptedModel({
        responses: [done, write("a.txt", "c1"), done],
      }),
      sandbox: fakeSandbox(),
      knowledge: kb.provider,
      permissions: { mode: "accept_edits" },
    });
    const first = await bot.run("chat", { store: sqlite(":memory:") });
    expect(first.status).toBe("completed");
    expect(kb.reads()).toBe(0);
    const second = await bot.run("write", { thread: first.thread });
    expect(second.status).toBe("completed");
    expect(kb.reads()).toBe(1);
  });
});
