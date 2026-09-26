import { describe, expect, test } from "bun:test";
import { writeFileSync } from "node:fs";
import { join } from "node:path";
import {
  type FakeSandbox,
  fakeSandbox,
  openThread,
  scriptedModel,
  sqlite,
} from "@threads/core";
import type { KnownEvent } from "../../core/src/log";
import { logOf } from "../../core/test/agent/kit";
import { unwrap } from "../../core/test/store/helpers";
import { dirOf } from "../../core/test/workspace/fixture";
import { codingAgent } from "../src";

// The preset at run time: what its permissions actually decide, that the sandbox never sees the
// host's credentials, and that the .threads guard still wins. A scripted model and the fake
// sandbox, so nothing here reaches Docker or a provider.

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

const decisions = (events: readonly KnownEvent[]) =>
  events.flatMap((e) =>
    e.type === "permission_decision"
      ? [[e.data.decision, e.data.source, e.data.rule_id ?? ""] as const]
      : [],
  );

const previews = (events: readonly KnownEvent[]) =>
  events.flatMap((e) => (e.type === "tool_result" ? [e.data.preview] : []));

const PIPELINE = "cd app && npm test 2>&1 | tail -50";

describe("what the preset's permissions decide", () => {
  test("a pipeline bash and an edit run without a challenge", async () => {
    const model = scriptedModel({
      responses: [
        use("bash", { command: PIPELINE }, "c1"),
        use("write", { path: "src/new.ts", content: "const x = 1;\n" }, "c2"),
        use(
          "edit",
          { path: "src/new.ts", old_string: "1", new_string: "2" },
          "c3",
        ),
        say("done"),
      ],
    });
    const result = await codingAgent({
      model,
      sandbox: fakeSandbox({ tools: { cd: { output: "" } } }),
    }).run("go", { store: sqlite(":memory:") });
    expect(result.status).toBe("completed");
    // The pipeline is a bash(*) allow; the edits are the accept_edits mode's own column.
    expect(decisions(await logOf(result.thread))).toEqual([
      ["allow", "policy", "bash(*)"],
      ["allow", "mode", ""],
      ["allow", "mode", ""],
    ]);
  });

  test("a deny override still denies, before bash(*) is consulted", async () => {
    const model = scriptedModel({
      responses: [
        use("bash", { command: "rm -rf build" }, "c1"),
        say("stopped"),
      ],
    });
    const result = await codingAgent({
      model,
      sandbox: fakeSandbox(),
      permissions: {
        mode: "accept_edits",
        allow: ["bash(*)"],
        deny: ["bash(rm:*)"],
      },
    }).run("go", { store: sqlite(":memory:") });
    expect(decisions(await logOf(result.thread))).toEqual([
      ["deny", "policy", "bash(rm:*)"],
    ]);
  });

  test("plan mode denies bash and write, and allows todo_write", async () => {
    const model = scriptedModel({
      responses: [
        use("bash", { command: "npm test" }, "c1"),
        use("write", { path: "src/new.ts", content: "x" }, "c2"),
        use(
          "todo_write",
          { todos: [{ id: "1", content: "look around", status: "pending" }] },
          "c3",
        ),
        say("read-only"),
      ],
    });
    const result = await codingAgent({
      model,
      sandbox: fakeSandbox(),
      permissions: { mode: "plan" },
    }).run("go", { store: sqlite(":memory:") });
    expect(decisions(await logOf(result.thread))).toEqual([
      ["deny", "mode", ""],
      ["deny", "mode", ""],
      ["allow", "mode", ""],
    ]);
  });

  test("a memory write parks, and the challenge names the option to change", async () => {
    const store = sqlite(":memory:");
    const model = scriptedModel({
      responses: [use("save_memory", { text: "the tests run with bun" }, "c1")],
    });
    const result = await codingAgent({ model, sandbox: fakeSandbox() }).run(
      "remember",
      {
        store,
      },
    );
    expect(result.status).toBe("parked");
    const thread = unwrap(await openThread(store, result.thread.id));
    const [pending] = unwrap(await thread.pendingApprovals());
    expect(pending?.reason).toBe(
      'memory_write is "ask": approve this call, or set memory_write to "allow_principal" or "allow"',
    );
  });
});

describe("the invariants the preset must not weaken", () => {
  test("4: the host's credentials never reach the sandbox", async () => {
    const had = process.env["ANTHROPIC_API_KEY"];
    process.env["ANTHROPIC_API_KEY"] = "sk-ant-not-a-real-key";
    const sandbox: FakeSandbox = fakeSandbox({
      tools: { printenv: { output: "" } },
    });
    try {
      const model = scriptedModel({
        responses: [
          use("bash", { command: "printenv" }, "c1"),
          say("nothing there"),
        ],
      });
      const result = await codingAgent({ model, sandbox }).run("go", {
        store: sqlite(":memory:"),
      });
      expect(result.status).toBe("completed");
      const [exec] = sandbox.execs();
      if (exec === undefined)
        throw new Error("the bash call reached the sandbox");
      // Names, not values: a CI log masks a value it knows, so a leak has to be named to read.
      expect(Object.keys(exec.env)).not.toContain("ANTHROPIC_API_KEY");
      expect(Object.keys(exec.env)).toEqual([]);
    } finally {
      if (had === undefined) delete process.env["ANTHROPIC_API_KEY"];
      else process.env["ANTHROPIC_API_KEY"] = had;
    }
  });

  test("7: bash(*) is consulted after the .threads guard, which denies both ways in", async () => {
    const model = scriptedModel({
      responses: [
        use("bash", { command: "echo x > .threads/agents.ts" }, "c1"),
        use("write", { path: ".threads/x", content: "x" }, "c2"),
        say("refused"),
      ],
    });
    const result = await codingAgent({ model, sandbox: fakeSandbox() }).run(
      "go",
      {
        store: sqlite(":memory:"),
      },
    );
    expect(decisions(await logOf(result.thread))).toEqual([
      ["deny", "self_config_guard", ""],
      ["deny", "self_config_guard", ""],
    ]);
  });
});

describe("a workspace the preset passes through", () => {
  test("the sandbox starts with the copied files, and a changed copy fails closed", async () => {
    const root = dirOf({ "src/cli.ts": "export const flags = [];\n" });
    const model = () =>
      scriptedModel({
        responses: [use("read", { path: "src/cli.ts" }, "c1"), say("read it")],
      });
    const store = sqlite(":memory:");
    const preset = (sandbox = fakeSandbox()) =>
      codingAgent({ model: model(), sandbox, workspace: { localDir: root } });
    const result = await preset().run("what does the CLI do?", { store });
    expect(result.status).toBe("completed");
    expect(previews(await logOf(result.thread))[0]).toContain(
      "export const flags = [];",
    );

    // The copy is pinned as bytes, so continuing the thread after ./app changed fails closed.
    writeFileSync(
      join(root, "src/cli.ts"),
      "export const flags = ['--json'];\n",
    );
    const again = preset().run("and now?", { store, thread: result.thread });
    await expect(again).rejects.toMatchObject({
      code: "invalid_config",
      message:
        "the workspace changed since this thread started; start a new thread",
    });
  });
});
