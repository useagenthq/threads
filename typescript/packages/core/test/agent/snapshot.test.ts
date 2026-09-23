import { describe, expect, test } from "bun:test";
import {
  agent,
  fakeSandbox,
  manifestHash,
  manifestOf,
  type RunResult,
  scriptedModel,
  sqlite,
} from "../../src";
import { openStore } from "../../src/agent/sqlite";
import { knownEvents, reduce } from "../../src/reduce";
import { unwrap } from "../store/helpers";

// The end-of-turn snapshot policy: after a turn that ran a tool whose
// class is not read_only, the sandbox is captured through captureSnapshot (ledgered and
// image-verified, ), and only a verified capture becomes a snapshot event.

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
const utf8 = new TextEncoder();
const A = "/workspace/a.txt";

async function outcome<T>(result: RunResult<T>) {
  const { log } = await openStore(result.thread.store);
  const chain = unwrap(log.read(result.thread.branch));
  return {
    snapshots: knownEvents(chain).flatMap((e) =>
      e.type === "snapshot" ? [e.data] : [],
    ),
    forkPoints: reduce(chain, Date.now()).fork_points.length,
    rows: unwrap(log.ledger.rows()).map((r) => [r.kind, r.state]),
  };
}

function writer(responses: readonly unknown[]) {
  const sandbox = fakeSandbox();
  const bot = agent({
    model: scriptedModel({ responses: [...responses] }),
    sandbox,
    permissions: { mode: "accept_edits" },
  });
  return { sandbox, bot };
}

describe("end-of-turn snapshots go through captureSnapshot", () => {
  test("a turn that wrote a file ends with a verified snapshot: a fork point", async () => {
    const { bot } = writer([
      use("write", { path: "a.txt", content: "A" }, "c1"),
      say("done"),
    ]);
    const result = await bot.run("write", { store: sqlite(":memory:") });
    expect(result.status).toBe("completed");
    const got = await outcome(result);
    expect(got.snapshots.map((s) => s.manifest_hash)).toEqual([
      manifestHash(manifestOf(new Map([[A, utf8.encode("A")]]))),
    ]);
    expect(got.forkPoints).toBe(1);
    expect(got.rows).toEqual([
      ["sandbox", "live"],
      ["snapshot", "live"],
      ["sandbox", "released"],
    ]);
  });

  test("a read-only turn takes no snapshot", async () => {
    const { bot } = writer([
      use("read_tool_result", { call_id: "x", offset: 0, length: 1 }, "c1"),
      say("ok"),
    ]);
    const got = await outcome(
      await bot.run("read", { store: sqlite(":memory:") }),
    );
    expect(got.snapshots).toEqual([]);
  });

  test("A→B just before the capture, B→A right after: refused, nothing recorded", async () => {
    const { sandbox, bot } = writer([
      use("write", { path: "a.txt", content: "A" }, "c1"),
      say("done"),
    ]);
    sandbox.aroundNextCapture({
      before: (tree) => tree.set(A, utf8.encode("B")),
      after: (tree) => tree.set(A, utf8.encode("A")),
    });
    const result = await bot.run("write", { store: sqlite(":memory:") });
    expect(result.status).toBe("completed");
    const got = await outcome(result);
    expect(got.snapshots).toEqual([]);
    expect(got.forkPoints).toBe(0);
    expect(got.rows).toEqual([
      ["sandbox", "live"],
      ["snapshot", "released"],
      ["sandbox", "released"],
    ]);
  });
});
