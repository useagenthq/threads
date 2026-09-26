// A coding agent in one call: Claude Sonnet 5, a Docker sandbox, memory, and the edits back as a
// diff. The real thing is one line, `codingAgent({ workspace: { localDir: "./app" } })`.
// Needs: nothing (a scripted model and a fake sandbox stand in for Claude and Docker here, so
//        this runs with no API key, no network and no daemon).
// Run: cd typescript && bun examples/coding-agent.ts

import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { codingAgent } from "@threads/coding";
import { fakeSandbox, scriptedModel, sqlite } from "@threads/core";

const usage = { input_tokens: 10, output_tokens: 2 };
const call = (name: string, input: Record<string, unknown>, id: string) => ({
  content: [{ type: "tool_use", call_id: id, name, input }],
  stop_reason: "tool_use",
  usage,
});

/** The directory you would point `localDir` at: your project, copied into /workspace. */
function project(): string {
  const root = mkdtempSync(join(tmpdir(), "app-"));
  mkdirSync(join(root, "src"));
  writeFileSync(join(root, "src/cli.ts"), "export const flags = [];\n");
  return root;
}

export async function main(): Promise<string> {
  const coder = codingAgent({
    // Everything below stands in for the default Claude Sonnet 5 and docker() so the example
    // runs anywhere; drop these two arguments and it is the real thing.
    model: scriptedModel({
      responses: [
        call("read", { path: "src/cli.ts" }, "c1"),
        call(
          "edit",
          { path: "src/cli.ts", old_string: "[]", new_string: '["--json"]' },
          "c2",
        ),
        call("bash", { command: "npm test 2>&1 | tail -5" }, "c3"),
        {
          content: [
            { type: "text", text: "Added --json to the CLI; its tests pass." },
          ],
          stop_reason: "end_turn",
          usage,
        },
      ],
    }),
    sandbox: fakeSandbox({ tools: { npm: { output: "1 passing\n" } } }),
    workspace: { localDir: project() },
  });

  const result = await coder.run(
    "Add a --json flag to the CLI and run its tests",
    {
      store: sqlite(":memory:"),
    },
  );
  // The real agent ends its answer with a unified diff of everything it changed, which you apply
  // with `git apply`. Your files on disk are untouched: /workspace was a copy.
  return result.status === "completed" ? result.output : result.status;
}

if (import.meta.main) console.log(await main());
// Output: Added --json to the CLI; its tests pass.
