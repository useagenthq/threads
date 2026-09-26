import { afterEach, describe, expect, test } from "bun:test";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { sqlite } from "@threads/core";
import { run } from "../src";
import { connects, support } from "./fixtures/eval-agents";
import { support as simulated } from "./fixtures/eval-simulated";

// `threads eval`, in process, so the test preload's model-request guard covers it (spec lane 22,
// test 10). No test starts a `threads` subprocess.

const dirs: string[] = [];
afterEach(() => {
  for (const d of dirs.splice(0)) rmSync(d, { recursive: true, force: true });
});

const FIXTURES = join(import.meta.dir, "fixtures");
const NOTE =
  " (framework checks only; pass --agent to detect changes to your agents)";

async function casesWith(...names: readonly string[]): Promise<string> {
  const dir = mkdtempSync(join(tmpdir(), "threads-eval-cli-"));
  dirs.push(dir);
  for (const name of names) {
    const ran = await support().run("Where is order 42?", {
      store: sqlite(":memory:"),
    });
    const saved = await ran.thread.saveCase(name, {
      expect: { must: [{ type: "tool_call", data: { name: "lookup_order" } }] },
      externalEffects: "stub",
      rubric: ["Quotes the 30-day window"],
      dir,
    });
    if (!saved.ok) throw new Error(saved.error.message);
  }
  return dir;
}

/** One saved case whose live run is a conversation with a model playing the user. */
async function simulatedCase(): Promise<string> {
  const dir = mkdtempSync(join(tmpdir(), "threads-eval-cli-"));
  dirs.push(dir);
  const ran = await simulated().run("Where is order 42?", {
    store: sqlite(":memory:"),
  });
  const saved = await ran.thread.saveCase("a-case", {
    expect: { must: [{ type: "tool_call", data: { name: "lookup_order" } }] },
    externalEffects: "stub",
    rubric: ["Quotes the 30-day window"],
    simulate: {
      kind: "model",
      persona: "A polite but persistent customer.",
      goal: "Get a refund, or a clear reason why not.",
      maxMessages: 3,
    },
    dir,
  });
  if (!saved.ok) throw new Error(saved.error.message);
  return dir;
}

async function cli(argv: readonly string[]) {
  const got = { out: "", err: "" };
  const code = await run(argv, {
    out: (t) => {
      got.out += t;
    },
    bytes: () => {},
    err: (t) => {
      got.err += t;
    },
  });
  return { code, ...got };
}

describe("threads eval", () => {
  test("without --agent: framework checks only, exit 0, one line per case then the summary", async () => {
    const dir = await casesWith("a-case", "b-case");
    const out = join(dir, "report.json");
    const got = await cli(["eval", "--cases", dir, "--out", out]);
    expect(got.code).toBe(0);
    expect(got.out).toBe(
      `PASS a-case\nPASS b-case\n2 passed, 0 failed${NOTE}\n`,
    );
    const report = readFileSync(out, "utf8");
    expect(report.endsWith("}\n")).toBe(true);
    expect(JSON.parse(report).summary).toBe(`2 passed, 0 failed${NOTE}`);
  });

  test("--case filters; a failing case exits 1", async () => {
    const dir = await casesWith("a-case", "b-case");
    const path = join(dir, "b-case", "sandbox.json");
    const sandbox = JSON.parse(readFileSync(path, "utf8"));
    sandbox.results[0].preview = "order 42: lost";
    writeFileSync(path, JSON.stringify(sandbox));
    expect((await cli(["eval", "--cases", dir, "--case", "a-case"])).code).toBe(
      0,
    );
    const failed = await cli(["eval", "--cases", dir]);
    expect(failed.code).toBe(1);
    expect(failed.out).toContain(
      "FAIL b-case rerun: event 5 is tool_result, recorded tool_result",
    );
  });

  test("--agent adds drift; stale fails only with --strict", async () => {
    const dir = await casesWith("a-case");
    const agents = join(FIXTURES, "eval-agents.ts");
    const same = await cli(["eval", "--cases", dir, "--agent", agents]);
    expect([same.code, same.out]).toEqual([
      0,
      "PASS a-case\n1 passed, 0 failed\n",
    ]);
    const mcp = join(FIXTURES, "eval-mcp.ts");
    const stale = await cli(["eval", "--cases", dir, "--agent", mcp]);
    expect(stale.code).toBe(0);
    expect(stale.out).toContain("PASS a-case (drift: unchecked mcp:jira)");
    expect(connects.n).toBe(0);
    const changed = join(FIXTURES, "eval-changed.ts");
    const drifted = await cli(["eval", "--cases", dir, "--agent", changed]);
    expect(drifted.code).toBe(0);
    expect(drifted.out).toBe(
      "STALE a-case drift: prompt, tools (-lookup_order)\n0 passed, 0 failed, 1 stale\n",
    );
    const strict = await cli([
      "eval",
      "--cases",
      dir,
      "--agent",
      changed,
      "--strict",
    ]);
    expect(strict.code).toBe(1);
  });

  test("a module that throws at import is exit 2, naming it; --live needs --agent", async () => {
    const dir = await casesWith("a-case");
    const thrown = await cli([
      "eval",
      "--cases",
      dir,
      "--agent",
      join(FIXTURES, "eval-throws.ts"),
    ]);
    expect(thrown.code).toBe(2);
    expect(thrown.err).toContain(
      "eval-throws.ts: import failed: JIRA_MCP_URL_FOR_THREADS_TESTS is not set",
    );
    const live = await cli(["eval", "--cases", dir, "--live"]);
    expect([live.code, live.err]).toEqual([
      2,
      "threads eval --live needs --agent <module>\n",
    ]);
    const noJudge = await cli([
      "eval",
      "--cases",
      dir,
      "--live",
      "--agent",
      join(FIXTURES, "eval-no-judge.ts"),
    ]);
    expect(noJudge.code).toBe(2);
    expect(noJudge.err).toContain("export judge from");
    expect((await cli(["eval", "--cases", join(dir, "nope")])).code).toBe(2);
  });

  test("a simulated case needs the module to export user, and the preflight says so", async () => {
    const dir = await simulatedCase();
    const noUser = await cli([
      "eval",
      "--cases",
      dir,
      "--live",
      "--agent",
      join(FIXTURES, "eval-agents.ts"),
    ]);
    expect([noUser.code, noUser.err.trim()]).toEqual([
      2,
      `export user from ${join(FIXTURES, "eval-agents.ts")}`,
    ]);
    const got = await cli([
      "eval",
      "--cases",
      dir,
      "--live",
      "--agent",
      join(FIXTURES, "eval-simulated.ts"),
    ]);
    expect(got.out.split("\n")[0]).toBe(
      'live: 1 cases (1 simulated, up to 3 user messages), budget {"max_model_requests":20} per conversation and per judge run',
    );
    expect(got.code).toBe(0);
    expect(got.out).toContain("a-case: 2 messages, user done");
  });

  test("--live grades with the module's judge; without --store its threads are not kept", async () => {
    const dir = await casesWith("a-case");
    const got = await cli([
      "eval",
      "--cases",
      dir,
      "--live",
      "--agent",
      join(FIXTURES, "eval-agents.ts"),
    ]);
    expect(got.code).toBe(0);
    expect(got.out.split("\n")).toEqual([
      'live: 1 cases, up to 2 model runs (agent + judge), budget {"max_model_requests":10} per run',
      "PASS a-case",
      "judge threads were not kept; pass --store to keep them",
      "1 passed, 0 failed; 3 model calls (2 agent, 0 user, 1 judge), cost unknown",
      "",
    ]);
  });
});
