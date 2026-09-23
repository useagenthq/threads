import { afterEach, describe, expect, test } from "bun:test";
import {
  cpSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readdirSync,
  rmSync,
  utimesSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { agent, openThread, scriptedModel, sqlite } from "@threads/core";
import { storeConnection, tenantStore } from "@threads/core/host";
import { run, type Served } from "../src";

// The CLI over a real store directory: export, import, repair, delete, gc, timeline and dev.

const dirs: string[] = [];
afterEach(() => {
  for (const d of dirs.splice(0)) rmSync(d, { recursive: true, force: true });
});

function temp(): string {
  const d = mkdtempSync(join(tmpdir(), "threads-cli-"));
  dirs.push(d);
  return d;
}

type Captured = { out: string; err: string; bytes: Uint8Array[] };

async function cli(argv: readonly string[], serving?: (s: Served) => void) {
  const got: Captured = { out: "", err: "", bytes: [] };
  const code = await run(
    argv,
    {
      out: (t) => {
        got.out += t;
      },
      bytes: (b) => {
        got.bytes.push(b);
      },
      err: (t) => {
        got.err += t;
      },
    },
    serving,
  );
  return { code, ...got };
}

const say = (text: string) => ({
  content: [{ type: "text", text }],
  stop_reason: "end_turn",
  usage: { input_tokens: 1, output_tokens: 1 },
});

async function seeded(dir: string) {
  const bot = agent({ model: scriptedModel({ responses: [say("Hi")] }) });
  const result = await bot.run("Hello", { store: sqlite(dir) });
  return result.thread;
}

describe("threads export, import, repair, timeline", () => {
  test("an exported branch imports into another store byte for byte", async () => {
    const a = temp();
    const thread = await seeded(a);
    const exported = await cli(["export", thread.branch, "--store", a]);
    expect(exported.code).toBe(0);
    const bytes = exported.bytes[0] ?? new Uint8Array();
    expect(new TextDecoder().decode(bytes)).toContain('"type":"user_input"');
    const b = temp();
    const file = join(b, "branch.jsonl");
    writeFileSync(file, bytes);
    // The requests an export's model_requests name travel with it.
    cpSync(join(a, "artifacts"), join(b, "store", "artifacts"), {
      recursive: true,
    });
    const imported = await cli(["import", file, "--store", join(b, "store")]);
    expect(imported.err).toBe("");
    expect(imported.code).toBe(0);
    expect(imported.out.trim()).toBe(thread.branch);
    const again = await cli([
      "export",
      thread.branch,
      "--store",
      join(b, "store"),
    ]);
    expect(again.bytes[0]).toEqual(bytes);
    const repaired = await cli([
      "repair",
      thread.branch,
      "--store",
      join(b, "store"),
    ]);
    expect(repaired.code).toBe(0);
    const steps = await cli(["timeline", thread.id, "--store", a]);
    expect(steps.out.trim().split("\n").length).toBeGreaterThan(2);
  });

  test("an unknown branch is an error with its code; bad usage is 2", async () => {
    const a = temp();
    const missing = await cli(["export", crypto.randomUUID(), "--store", a]);
    expect(missing.code).toBe(1);
    expect(missing.err).toContain("branch_not_found");
    expect((await cli(["nope"])).code).toBe(2);
  });
});

describe("threads delete and gc", () => {
  test("delete removes the thread and leaves a tombstone; another tenant can't", async () => {
    const a = temp();
    const thread = await seeded(a);
    const other = await cli([
      "delete",
      thread.id,
      "--store",
      a,
      "--tenant",
      "acme",
    ]);
    expect(other.code).toBe(1);
    expect(other.err).toContain("not_found");
    const done = await cli(["delete", thread.id, "--store", a]);
    expect(done.code).toBe(0);
    const opened = await openThread(tenantStore(sqlite(a), "local"), thread.id);
    expect(opened.ok).toBe(false);
    const { db } = await storeConnection(sqlite(a));
    expect(db.all("SELECT thread_id FROM tombstones", [])).toEqual([
      { thread_id: thread.id },
    ]);
  });

  test("delete takes its subagent threads along, never a handoff target", async () => {
    const a = temp();
    const use = (name: string, input: Record<string, unknown>, id: string) => ({
      content: [{ type: "tool_use", call_id: id, name, input }],
      stop_reason: "tool_use",
      usage: { input_tokens: 1, output_tokens: 1 },
    });
    const worker = agent({
      name: "worker",
      model: scriptedModel({ responses: [say("Done.")] }),
    });
    const billing = agent({
      name: "billing",
      model: scriptedModel({ responses: [say("Billing here.")] }),
    });
    const lead = agent({
      name: "lead",
      model: scriptedModel({
        responses: [
          use("spawn_agent", { agent: "worker", prompt: "Look." }, "s1"),
          use("handoff", { agent: "billing" }, "h1"),
        ],
      }),
      subagents: [worker],
      handoffs: [billing],
    });
    const result = await lead.run("Go.", { store: sqlite(a) });
    const { db } = await storeConnection(sqlite(a));
    expect(db.all("SELECT thread_id FROM threads", [])).toHaveLength(3);
    const done = await cli(["delete", result.thread.id, "--store", a]);
    expect(done.code).toBe(0);
    expect(db.all("SELECT thread_id FROM tombstones", [])).toHaveLength(2);
    const left = db.all(
      "SELECT e.line FROM events e JOIN branches b ON b.branch_id = e.branch_id WHERE e.seq = 1",
      [],
    );
    expect(left).toHaveLength(1);
    expect(JSON.stringify(left)).not.toContain('"subagent"');
  });

  test("gc sweeps old unreferenced artifacts and keeps referenced ones", async () => {
    const a = temp();
    await seeded(a);
    const stray = "a".repeat(64);
    const dir = join(a, "artifacts", "sha256", "aa");
    mkdirSync(dir, { recursive: true });
    const file = join(dir, stray);
    writeFileSync(file, "stray");
    const old = new Date(Date.now() - 30 * 86_400_000);
    utimesSync(file, old, old);
    const before = readdirSync(join(a, "artifacts", "sha256")).length;
    const swept = await cli(["gc", "--store", a]);
    expect(swept.code).toBe(0);
    expect(swept.out).toContain("removed 1");
    expect(existsSync(file)).toBe(false);
    expect(
      readdirSync(join(a, "artifacts", "sha256")).length,
    ).toBeGreaterThanOrEqual(before - 1);
  });
});

describe("threads dev", () => {
  test("loads the module, readies it, serves fetch and prints each webhook URL", async () => {
    process.env["THREADS_TEST_STORE"] = temp();
    let served: Served | undefined;
    const started = await cli(
      ["dev", join(import.meta.dir, "app.ts"), "--port", "0"],
      (s) => {
        served = s;
      },
    );
    expect(started.code).toBe(0);
    expect(started.out).toMatch(
      /slack: http:\/\/localhost:\d+\/channels\/slack\/events/,
    );
    if (served === undefined) throw new Error("dev serves");
    const response = await fetch(`${served.url}/v1/runs`, { method: "POST" });
    expect(response.status).toBe(401);
    await served.stop();
  });

  test("a module without a default host is refused", async () => {
    const d = temp();
    const file = join(d, "bad.ts");
    writeFileSync(file, "export default 1;\n");
    const result = await cli(["dev", file]);
    expect(result.code).toBe(1);
    expect(result.err).toContain("must export default host");
  });
});
