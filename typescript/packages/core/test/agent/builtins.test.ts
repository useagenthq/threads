import { describe, expect, test } from "bun:test";
import { z } from "zod";
import {
  agent,
  fakeSandbox,
  type RunResult,
  scriptedModel,
  secret,
  sqlite,
  tool,
} from "../../src";
import { openStore } from "../../src/agent/sqlite";
import type { KnownEvent } from "../../src/log";
import { knownEvents } from "../../src/reduce";
import { refReader, verifyRequests } from "../../src/render";
import { unwrap } from "../store/helpers";

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
const text = (b: Uint8Array): string => new TextDecoder().decode(b);

async function logOf<T>(result: RunResult<T>): Promise<readonly KnownEvent[]> {
  const { log, artifacts } = await openStore(result.thread.store);
  const events = knownEvents(unwrap(log.read(result.thread.branch)));
  unwrap(verifyRequests(events, refReader(artifacts)));
  return events;
}

const results = (events: readonly KnownEvent[]) =>
  events.flatMap((e) => (e.type === "tool_result" ? [e.data] : []));

describe("built-in sandbox tools", () => {
  test("pinned sorted by name before app tools; no sandbox pins only read_tool_result", async () => {
    const echo = tool({
      name: "echo",
      description: "Echo.",
      input: z.object({ text: z.string() }),
      runs: "host",
      effect: "read_only",
      execute: async ({ text }) => text,
    });
    const names = async (withSandbox: boolean) => {
      const bot = agent({
        model: scriptedModel({ responses: [say("ok")] }),
        tools: [echo],
        ...(withSandbox ? { sandbox: fakeSandbox() } : {}),
      });
      const events = await logOf(
        await bot.run("hi", { store: sqlite(":memory:") }),
      );
      const started = events.find((e) => e.type === "thread_started");
      return started?.type === "thread_started"
        ? started.data.tools.map((t) => `${t.name}:${t.effect_class}`)
        : [];
    };
    expect(await names(false)).toEqual([
      "read_tool_result:read_only",
      "echo:read_only",
    ]);
    expect(await names(true)).toEqual([
      "bash:sandbox_local",
      "edit:sandbox_local",
      "glob:read_only",
      "grep:read_only",
      "read:read_only",
      "read_tool_result:read_only",
      "write:sandbox_local",
      "echo:read_only",
    ]);
  });

  test("a provider that can't enforce egress needs egress: unenforced", async () => {
    const loose = {
      ...fakeSandbox(),
      info: { ...fakeSandbox().info, egress: "unenforced" as const },
    };
    const model = scriptedModel({ responses: [] });
    await expect(agent({ model, sandbox: loose }).check()).resolves.toMatchObject({
      ok: false,
      error: { code: "egress_policy_unsupported" },
    });
    await expect(
      agent({ model, sandbox: loose, egress: "unenforced" }).check(),
    ).resolves.toEqual({ ok: true, value: undefined });
  });

  test("write, read, edit, glob and grep run in the sandbox; bash execs sh -c with an empty env", async () => {
    const sandbox = fakeSandbox({ tools: { echo: { output: "hi\n" } } });
    const model = scriptedModel({
      responses: [
        use("write", { path: "src/app.ts", content: "const x = 1;\n" }, "c1"),
        use(
          "edit",
          { path: "src/app.ts", old_string: "1", new_string: "2" },
          "c2",
        ),
        use("read", { path: "src/app.ts" }, "c3"),
        use("glob", { pattern: "**/*.ts", path: "." }, "c4"),
        use("grep", { pattern: "x = [0-9]", path: "src" }, "c5"),
        use("bash", { command: "echo hi" }, "c6"),
        use(
          "edit",
          {
            path: "src/app.ts",
            old_string: "2",
            new_string: "3",
            expected_sha256: "0".repeat(64),
          },
          "c7",
        ),
        say("done"),
      ],
    });
    const bot = agent({
      model,
      sandbox,
      permissions: { mode: "accept_edits", allow: ["bash"] },
    });
    const result = await bot.run("go", { store: sqlite(":memory:") });
    expect(result.status).toBe("completed");
    const shown = results(await logOf(result)).map((r) => [
      r.call_id,
      r.is_error,
      r.preview,
    ]);
    expect(shown.slice(0, 5)).toEqual([
      ["c1", false, "wrote 13 bytes to src/app.ts"],
      ["c2", false, "wrote 13 bytes to src/app.ts"],
      ["c3", false, "1\tconst x = 2;\n2\t"],
      ["c4", false, "/workspace/src/app.ts"],
      ["c5", false, "/workspace/src/app.ts:1:const x = 2;\n"],
    ]);
    expect(JSON.parse(String(shown[5]?.[2]))).toMatchObject({
      exit_code: 0,
      stdout: "hi\n",
    });
    expect(shown[6]?.[1]).toBe(true);
    expect(String(shown[6]?.[2])).toContain("expected_sha256 mismatch");
    expect(sandbox.execs().at(-1)).toEqual({
      command: ["sh", "-c", "echo hi"],
      env: {},
    });
    expect(sandbox.files().map(text)).toEqual(["const x = 2;\n"]);
    expect(sandbox.creates()).toBe(1);
  });

  test("a second run on the thread reattaches to its live sandbox", async () => {
    const sandbox = fakeSandbox();
    const bot = agent({
      model: scriptedModel({
        responses: [
          use("write", { path: "a.txt", content: "kept" }, "c1"),
          say("one"),
          use("read", { path: "a.txt" }, "c2"),
          say("two"),
        ],
      }),
      sandbox,
      permissions: { mode: "accept_edits" },
    });
    const first = await bot.run("write", { store: sqlite(":memory:") });
    const second = await bot.run("read", { thread: first.thread });
    expect(results(await logOf(second)).at(-1)?.preview).toBe("1\tkept");
    expect(sandbox.creates()).toBe(1);
  });
});

describe("permission modes decide before any sandbox call", () => {
  const decisions = async (
    mode: "plan" | "dont_ask" | "accept_edits",
    call: ReturnType<typeof use>,
  ) => {
    const sandbox = fakeSandbox();
    const bot = agent({
      model: scriptedModel({ responses: [call, say("ok")] }),
      sandbox,
      permissions: { mode },
    });
    const result = await bot.run("go", { store: sqlite(":memory:") });
    const events = await logOf(result);
    const decided = events.flatMap((e) =>
      e.type === "permission_decision"
        ? [[e.data.decision, e.data.source]]
        : [],
    );
    return { status: result.status, decided, creates: sandbox.creates() };
  };

  test("plan denies a write; dont_ask denies a read outside the workspace", async () => {
    expect(
      await decisions("plan", use("write", { path: "a", content: "x" }, "c1")),
    ).toEqual({
      status: "completed",
      decided: [["deny", "mode"]],
      creates: 0,
    });
    expect(
      await decisions("dont_ask", use("read", { path: "/etc/passwd" }, "c1")),
    ).toEqual({
      status: "completed",
      decided: [["deny", "mode"]],
      creates: 0,
    });
  });

  test("a protected path asks even under accept_edits, and nothing runs before an approval", async () => {
    expect(
      await decisions(
        "accept_edits",
        use("write", { path: ".git/config", content: "x" }, "c1"),
      ),
    ).toEqual({
      status: "parked",
      decided: [["ask", "protected_path"]],
      creates: 0,
    });
  });
});

describe("L0 spill and secret redaction", () => {
  test("a large result keeps head, marker and tail; read_tool_result reads any range", async () => {
    const big = `${"a".repeat(40_000)}MIDDLE${"z".repeat(40_000)}`;
    const dump = tool({
      name: "dump",
      description: "Dump.",
      input: z.object({}),
      runs: "host",
      effect: "read_only",
      execute: async () => big,
    });
    const bot = agent({
      model: scriptedModel({
        responses: [
          use("dump", {}, "c1"),
          use(
            "read_tool_result",
            { call_id: "c1", offset: 40_000, length: 6 },
            "c2",
          ),
          say("ok"),
        ],
      }),
      tools: [dump],
    });
    const result = await bot.run("go", { store: sqlite(":memory:") });
    const [spilled, read] = results(await logOf(result));
    const marker = `\n[output truncated: 80006 bytes; read_tool_result(call_id="c1", offset, length) returns the rest]\n`;
    expect(spilled?.preview).toBe(
      `${"a".repeat(2048)}${marker}${"z".repeat(1024)}`,
    );
    expect(spilled?.ref?.bytes).toBe(80_006);
    expect(read?.preview).toBe("[bytes 40000-40006 of 80006]\nMIDDLE");
  });

  test("a revealed secret a host tool echoes is recorded by name only", async () => {
    process.env["THREADS_TEST_TOKEN"] = "tok-5f1e-very-secret";
    const token = secret("THREADS_TEST_TOKEN");
    const leak = tool({
      name: "leak",
      description: "Calls a service.",
      input: z.object({}),
      runs: "host",
      execute: async () => `Authorization: Bearer ${token.reveal()}`,
    });
    const bot = agent({
      model: scriptedModel({ responses: [use("leak", {}, "c1"), say("ok")] }),
      tools: [leak],
      permissions: { allow: ["leak"] },
    });
    const result = await bot.run("go", { store: sqlite(":memory:") });
    const events = await logOf(result);
    expect(results(events)[0]?.preview).toBe(
      "Authorization: Bearer [secret THREADS_TEST_TOKEN]",
    );
    expect(JSON.stringify(events)).not.toContain("tok-5f1e-very-secret");
  });
});
