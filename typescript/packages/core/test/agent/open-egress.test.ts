import { describe, expect, test } from "bun:test";
import { agent, fakeSandbox, scriptedModel, sqlite } from "../../src";
import { openStore } from "../../src/agent/sqlite";
import { knownEvents } from "../../src/reduce";
import { err, ok } from "../../src/result";
import type { Sandbox } from "../../src/sandbox/protocol";
import { unwrap } from "../store/helpers";

// Lane 16 A1: a sandbox_local built-in is sandbox_local only under deny-all egress. Under open
// egress bash, write, edit and notebook_edit pin unguarded, so a call whose outcome is in doubt
// parks rather than being settled by the sandbox.

const usage = { input_tokens: 1, output_tokens: 1 };
const use = (name: string, input: Record<string, unknown>) => ({
  content: [{ type: "tool_use", call_id: "c1", name, input }],
  stop_reason: "tool_use",
  usage,
});
const NOTEBOOK = JSON.stringify({
  cells: [{ id: "a", cell_type: "markdown", source: "# T", metadata: {} }],
  metadata: {},
  nbformat: 4,
  nbformat_minor: 5,
});

/** The fake sandbox, but every upload's answer is lost and every exec times out. */
function inDoubt(): Sandbox {
  const inner = fakeSandbox();
  return {
    ...inner,
    create: async (key, context) => {
      const made = await inner.create(key, context);
      if (!made.ok) return made;
      return ok({
        ...made.value,
        exec: async () => err({ code: "timeout", message: "deadline" }),
        download: async (path) =>
          ok(
            new TextEncoder().encode(
              path.endsWith(".ipynb") ? NOTEBOOK : "old",
            ),
          ),
        upload: async () => err({ code: "unavailable", message: "lost" }),
      });
    },
  };
}

const CALLS = {
  bash: use("bash", { command: "echo hi" }),
  edit: use("edit", { path: "a.txt", old_string: "old", new_string: "new" }),
  notebook_edit: use("notebook_edit", {
    path: "n.ipynb",
    cell_id: "a",
    new_source: "# U",
  }),
  write: use("write", { path: "a.txt", content: "x" }),
};

describe("open egress: the sandbox mutators are unguarded", () => {
  for (const [name, call] of Object.entries(CALLS))
    test(`${name} pins unguarded and an in-doubt call parks`, async () => {
      const bot = agent({
        model: scriptedModel({ responses: [call] }),
        sandbox: inDoubt(),
        egress: "unenforced",
        permissions: { allow: [name] },
      });
      const result = await bot.run("go", { store: sqlite(":memory:") });
      expect(result.status).toBe("parked");
      const { log } = await openStore(result.thread.store);
      const events = knownEvents(unwrap(await log.read(result.thread.branch)));
      const started = events.find((e) => e.type === "thread_started");
      if (started?.type !== "thread_started") throw new Error("no pin");
      const classes = started.data.tools
        .filter((t) => t.name in CALLS)
        .map((t) => t.effect_class);
      expect(classes).toEqual([
        "unguarded",
        "unguarded",
        "unguarded",
        "unguarded",
      ]);
      const kinds = events.map((e) => e.type);
      expect(kinds).toContain("effect_unknown");
      expect(kinds).not.toContain("effect_resolved");
      expect(kinds).not.toContain("tool_result");
    });
});
