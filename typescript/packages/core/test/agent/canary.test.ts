import { expect, test } from "bun:test";
import { z } from "zod";
import { agent, fakeSandbox, scriptedModel, secret, tool } from "../../src";
import { storeOf } from "../../src/agent/sqlite";
import { type ArtifactStore, LogStore, memoryArtifacts } from "../../src/store";
import { openBunSqlite } from "../../src/store/bun-sqlite";
import { unwrap } from "../store/helpers";

// F11.2 sandbox-credential-canary (v2, invariant 4): a host secret a
// credentialed host tool uses never reaches the sandbox env, argv or files, a prompt, an event
// line or any artifact.

const CANARY = "canary-7d41c9e2b0-do-not-leak";
const utf8 = new TextDecoder();

/** A memory artifact store that also keeps every byte string ever written. */
function recording(): {
  readonly store: ArtifactStore;
  readonly all: Uint8Array[];
} {
  const inner = memoryArtifacts();
  const all: Uint8Array[] = [];
  const store: ArtifactStore = {
    get: inner.get,
    put: (bytes) => {
      all.push(bytes.slice());
      return inner.put(bytes);
    },
    sink: () => {
      const sink = inner.sink();
      const chunks: Uint8Array[] = [];
      return {
        write: (chunk) => {
          chunks.push(chunk.slice());
          sink.write(chunk);
        },
        finish: () => {
          all.push(Buffer.concat(chunks));
          return sink.finish();
        },
        abort: sink.abort,
      };
    },
  };
  return { store, all };
}

const use = (name: string, input: Record<string, unknown>, id: string) => ({
  content: [{ type: "tool_use", call_id: id, name, input }],
  stop_reason: "tool_use",
  usage: { input_tokens: 10, output_tokens: 2 },
});

test("the canary secret is nowhere but the host tool that used it", async () => {
  process.env["THREADS_CANARY_SECRET"] = CANARY;
  const key = secret("THREADS_CANARY_SECRET");
  let usedOnHost = false;
  const deploy = tool({
    name: "deploy",
    description: "Deploy with the host credential.",
    input: z.object({}),
    runs: "host",
    execute: async () => {
      usedOnHost = key.reveal() === CANARY;
      // A careless host tool echoing its credential: the gateway redacts it (C5).
      return `deployed; auth ${key.reveal()}`;
    },
  });
  const sandbox = fakeSandbox({ tools: { env: { output: "PATH=/bin\n" } } });
  const artifacts = recording();
  const log = unwrap(
    LogStore.open(openBunSqlite(":memory:"), Date.now, artifacts.store),
  );
  const bot = agent({
    model: scriptedModel({
      responses: [
        use("deploy", {}, "c1"),
        use("bash", { command: "env" }, "c2"),
        use("write", { path: "notes.txt", content: "deployed" }, "c3"),
        {
          content: [{ type: "text", text: "done" }],
          stop_reason: "end_turn",
          usage: { input_tokens: 10, output_tokens: 2 },
        },
      ],
    }),
    // Interpolating the reference shows its name, never its value.
    instructions: `Deploy with ${key}.`,
    tools: [deploy],
    sandbox,
    permissions: { mode: "accept_edits", allow: ["deploy", "bash"] },
  });
  const result = await bot.run("ship it", {
    store: storeOf({ log, artifacts: artifacts.store }),
  });
  expect(result.status).toBe("completed");
  expect(usedOnHost).toBe(true);

  // Sandbox: env, argv and files.
  expect(sandbox.execs().length).toBeGreaterThan(0);
  for (const exec of sandbox.execs()) {
    expect(exec.env).toEqual({});
    expect(exec.command.join(" ")).not.toContain(CANARY);
  }
  for (const file of sandbox.files())
    expect(utf8.decode(file)).not.toContain(CANARY);
  // Every event line, and every artifact: rendered requests, results, spills.
  const lines = utf8.decode(unwrap(log.exportBranch(result.thread.branch)));
  expect(lines).toContain("secret(THREADS_CANARY_SECRET)");
  expect(lines).not.toContain(CANARY);
  expect(artifacts.all.length).toBeGreaterThan(0);
  for (const bytes of artifacts.all)
    expect(utf8.decode(bytes)).not.toContain(CANARY);
});
