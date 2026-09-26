import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { anthropic } from "@threads/anthropic";
import {
  type Agent,
  agent,
  fakeSandbox,
  localMemory,
  type Model,
  scriptedModel,
  type TeamAgent,
  tool,
} from "@threads/core";
import { docker } from "@threads/docker";
import { z } from "zod";
import { dryPin } from "../../core/src/agent/dry-pin";
import { CODING_INSTRUCTIONS, codingAgent } from "../src";

// What the preset promises (spec lane 31): it is agent() with five options chosen, so its pin is
// an agent() pin, and every default is one option away. Nothing here touches Docker or a model:
// the pin is drawn without setup (dryPin), which is also why no request can be dispatched.

// What the preset pins when nothing is passed, as both languages must pin it.
const GOLDEN: unknown = JSON.parse(
  readFileSync(
    join(
      import.meta.dir,
      "../../../../spec/conformance/vectors/coding-preset.json",
    ),
    "utf8",
  ),
);

const usage = { input_tokens: 10, output_tokens: 2 };
const scripted = (): Model =>
  scriptedModel({
    responses: [
      {
        content: [{ type: "text", text: "done" }],
        stop_reason: "end_turn",
        usage,
      },
    ],
  });

/** The hand-written agent() the preset must be indistinguishable from. */
const written = (model: Model) =>
  agent({
    model,
    sandbox: fakeSandbox(),
    permissions: { mode: "accept_edits", allow: ["bash(*)"] },
    memory: localMemory(),
    instructions: CODING_INSTRUCTIONS,
  });

const pinOf = (handle: object) => dryPin(handle).started;
/** The pinned permission policy; the pin always has one, so an absent one is a broken pin. */
const policyOf = (handle: object) => {
  const found = pinOf(handle).policy?.permissions;
  if (found === undefined)
    throw new Error("every pin carries a permission policy");
  return found;
};
const toolNames = (handle: object) =>
  pinOf(handle)
    .tools.map((t) => t.name)
    .toSorted();

describe("codingAgent", () => {
  test("its pin is the equivalent agent() pin, config_hash included", () => {
    const model = scripted();
    const preset = pinOf(codingAgent({ model, sandbox: fakeSandbox() }));
    const hand = pinOf(written(model));
    expect(preset.config_hash).toBe(hand.config_hash);
    // Byte-equal, not just equal-hashed: the ids a new thread mints are not in the dry pin.
    expect(JSON.stringify(preset)).toBe(JSON.stringify(hand));
  });

  test("the defaults are Claude Sonnet 5, Docker, bash(*) and local memory", () => {
    const pin = pinOf(codingAgent());
    expect(pin.model.name).toBe("claude-sonnet-5");
    expect(pin.model.provider).toBe("anthropic");
    const permissions = policyOf(codingAgent());
    expect(permissions.mode).toBe("accept_edits");
    expect(permissions.allow).toEqual(["bash(*)"]);
    // Kept, not replaced: the protected paths and the bypass refusal are still the built-ins.
    expect(permissions.allow_bypass).toBe(false);
    expect(permissions.protected_paths.length).toBeGreaterThan(0);
    expect(pin.instructions).toBe(CODING_INSTRUCTIONS);
    // Docker's provider and egress are hashed, not in the pin's fields, so the hand-written
    // agent() is what says which sandbox the preset built. cpus and memory_mb are not pinned at
    // all: the live Docker job reads them back off the container it creates.
    const hand = pinOf(
      agent({
        model: anthropic("claude-sonnet-5"),
        sandbox: docker({ cpus: 2, memoryMb: 4096 }),
        permissions: { mode: "accept_edits", allow: ["bash(*)"] },
        memory: localMemory(),
        instructions: CODING_INSTRUCTIONS,
      }),
    );
    expect(pin.config_hash).toBe(hand.config_hash);
    expect(JSON.stringify(pin)).toBe(JSON.stringify(hand));
  });

  test("the pinned tools are the sandbox tools, memory and todo_write", () => {
    const names = toolNames(
      codingAgent({ model: scripted(), sandbox: fakeSandbox() }),
    );
    expect(names).toEqual([
      "bash",
      "edit",
      "forget_memory",
      "glob",
      "grep",
      "ls",
      "notebook_edit",
      "read",
      "read_tool_result",
      "save_memory",
      "search_memory",
      "todo_write",
      "write",
    ]);
  });

  test("the instructions, model and permissions are what both languages pin", () => {
    const permissions = policyOf(codingAgent());
    expect(GOLDEN).toEqual({
      instructions: CODING_INSTRUCTIONS,
      model: pinOf(codingAgent()).model.name,
      permissions: { mode: permissions.mode, allow: permissions.allow },
    });
  });
});

describe("codingAgent overrides", () => {
  const base = { model: scripted(), sandbox: fakeSandbox() } as const;

  test("permissions replace the preset's object, bash(*) with it", () => {
    const permissions = policyOf(
      codingAgent({ ...base, permissions: { mode: "plan" } }),
    );
    expect(permissions.mode).toBe("plan");
    expect(permissions.allow).toEqual([]);
  });

  test("instructions replace the default, and extending keeps it", () => {
    const extended = `${CODING_INSTRUCTIONS}\n\nThe repo is acme/app.`;
    expect(
      pinOf(codingAgent({ ...base, instructions: extended })).instructions,
    ).toBe(extended);
  });

  test("memory: undefined pins no memory tools", () => {
    const names = toolNames(codingAgent({ ...base, memory: undefined }));
    expect(names).not.toContain("save_memory");
    expect(names).toContain("bash");
  });

  test("sandbox: undefined pins no sandbox tools", () => {
    const names = toolNames(codingAgent({ ...base, sandbox: undefined }));
    expect(names).not.toContain("bash");
    expect(names).not.toContain("read");
    expect(names).toContain("todo_write");
  });

  test("a model override is the model that is pinned", () => {
    expect(pinOf(codingAgent(base)).model.provider).not.toBe("anthropic");
  });

  test("the overloads: output narrows, tools carry deps, team leads", () => {
    // Checked by tsc (bun run typecheck compiles this file), asserted here so it also runs.
    type Deps = { readonly user: string };
    const ping = tool<{ readonly q: string }, string, Deps>({
      name: "ping",
      description: "Ask the deps.",
      input: z.object({ q: z.string() }),
      effect: "read_only",
      execute: async ({ q }, ctx) => `${q}:${ctx.deps?.user}`,
    });
    const Answer = z.object({ diff: z.string() });
    const plain: Agent<undefined, string> = codingAgent();
    const withDeps: Agent<Deps, string> = codingAgent({
      ...base,
      tools: [ping],
    });
    const typed: Agent<undefined, { diff: string }> = codingAgent({
      ...base,
      output: Answer,
    });
    const lead: TeamAgent<undefined, string> = codingAgent({
      ...base,
      team: [],
    });
    const dropped: Agent<undefined, string> = codingAgent({
      ...base,
      memory: undefined,
      sandbox: undefined,
    });
    expect([plain, withDeps, typed, lead, dropped].map((a) => a.name)).toEqual(
      Array.from({ length: 5 }, () => "agent"),
    );
  });

  test("an egress allowlist is refused, as it is for any agent", async () => {
    const refused = await codingAgent({ ...base, egress: ["x.com"] }).check();
    expect(refused.ok).toBe(false);
    if (refused.ok) throw new Error("egress allowlists are unsupported");
    expect(refused.error.code).toBe("egress_policy_unsupported");
  });
});
