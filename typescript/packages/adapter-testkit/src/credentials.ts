import { describe, expect, test } from "bun:test";
import { existsSync, mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  type AgentOptions,
  agent,
  ConfigError,
  openThread,
  type RunResult,
  type Secret,
  scriptedModel,
  sqlite,
  tool,
} from "@threads/core";
import { z } from "zod";

// Lane 09's per-factory cases, shared by every single-account adapter: the credential defaults
// to secret("<ENV>"), a missing one fails check() and the run at setup with a message naming the
// option and the variable, a fixed environment is picked up by the same agent, and the value is
// redacted from tool results whether it was given or defaulted. Nothing here touches a network.

export type CredentialCase = {
  /** The factory's name, as the message and the redaction label spell it. */
  readonly factory: string;
  readonly env: string;
  /** The adapter built with this apiKey option (undefined: the default), in its agent slot. */
  readonly slot: (
    apiKey: string | Secret | undefined,
  ) => Pick<AgentOptions<undefined, string>, "fallback" | "sandbox" | "memory">;
};

const usage = { input_tokens: 1, output_tokens: 1 };
const say = {
  content: [{ type: "text", text: "ok" }],
  stop_reason: "end_turn",
  usage,
};

/** A run whose one tool call echoes `value`: its recorded result text, and every stored event. */
async function echoed(
  value: string,
  slot: CredentialCase["slot"],
  apiKey: string | undefined,
): Promise<{ readonly preview: string; readonly log: string }> {
  const echo = tool({
    name: "echo",
    description: "Echo a value.",
    input: z.object({}),
    effect: "read_only",
    execute: async () => `value=${value}`,
  });
  const bot = agent({
    model: scriptedModel({
      responses: [
        {
          content: [
            { type: "tool_use", call_id: "c1", name: "echo", input: {} },
          ],
          stop_reason: "tool_use",
          usage,
        },
        say,
      ],
    }),
    tools: [echo],
    permissions: { mode: "bypass" },
    ...slot(apiKey),
  });
  return recorded(await bot.run("go", { store: sqlite(":memory:") }));
}

async function recorded(
  result: RunResult<string>,
): Promise<{ readonly preview: string; readonly log: string }> {
  const thread = await openThread(result.thread.store, result.thread.id, {
    branchId: result.thread.branch,
  });
  if (!thread.ok) throw new Error(thread.error.message);
  const timeline = await thread.value.timeline();
  if (!timeline.ok) throw new Error(timeline.error.message);
  const events = timeline.value.entries.map((e) => e.event);
  // A stored event may be of a type this build doesn't know: its data is parsed, not assumed.
  const found = events.find((e) => e.type === "tool_result");
  const data = z.object({ preview: z.string() }).safeParse(found?.data);
  return {
    preview: data.success ? data.data.preview : "",
    log: JSON.stringify(events),
  };
}

/** Runs `body` with `env` set to `value` (unset when undefined), then restores it. */
async function withEnv<T>(
  env: string,
  value: string | undefined,
  body: () => Promise<T>,
): Promise<T> {
  const before = process.env[env];
  if (value === undefined) delete process.env[env];
  else process.env[env] = value;
  try {
    return await body();
  } finally {
    if (before === undefined) delete process.env[env];
    else process.env[env] = before;
  }
}

export function credentialCases(c: CredentialCase): void {
  const message = `${c.factory}: set apiKey or ${c.env}`;
  const bot = () =>
    agent({
      model: scriptedModel({ responses: [say] }),
      ...c.slot(undefined),
    });

  describe(`${c.factory}() credentials`, () => {
    test("missing: the factory and agent() succeed; check() and the run fail at setup", async () => {
      await withEnv(c.env, undefined, async () => {
        const checked = await bot().check();
        expect(checked).toEqual({
          ok: false,
          error: { code: "missing_secret", message },
        });
        // The run fails before its store is opened, so nothing is appended.
        const dir = join(mkdtempSync(join(tmpdir(), "threads-cred-")), "store");
        const run = bot().run("go", { store: sqlite(dir) });
        await expect(run).rejects.toBeInstanceOf(ConfigError);
        await expect(run).rejects.toMatchObject({
          code: "missing_secret",
          message,
        });
        expect(existsSync(dir)).toBe(false);
      });
    });

    test("an empty value counts as unset", async () => {
      await withEnv(c.env, "", async () => {
        expect(await bot().check()).toMatchObject({ ok: false });
      });
    });

    test("a fixed environment is picked up by the same agent", async () => {
      const same = bot();
      await withEnv(c.env, undefined, async () => {
        expect((await same.check()).ok).toBe(false);
      });
      await withEnv(c.env, `${c.factory}-retry-key`, async () => {
        expect(await same.check()).toEqual({ ok: true, value: undefined });
        const run = await same.run("go", { store: sqlite(":memory:") });
        expect(run.status).toBe("completed");
      });
    });

    test("a given key and a defaulted key are redacted from tool results and never stored", async () => {
      const given = `${c.factory}-given-key-7f3a`;
      const label = `[secret ${c.factory}.apiKey]`;
      const explicit = await echoed(given, c.slot, given);
      expect(explicit.preview).toBe(`value=${label}`);
      expect(explicit.log).not.toContain(given);
      expect(JSON.stringify(c.slot(given))).not.toContain(given);
      const defaulted = `${c.factory}-env-key-9b1c`;
      await withEnv(c.env, defaulted, async () => {
        const fromEnv = await echoed(defaulted, c.slot, undefined);
        expect(fromEnv.preview).toBe(`value=${label}`);
        expect(fromEnv.log).not.toContain(defaulted);
      });
    });
  });
}
