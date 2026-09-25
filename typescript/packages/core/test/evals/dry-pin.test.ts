import { describe, expect, test } from "bun:test";
import { z } from "zod";
import {
  type Agent,
  agent,
  extension,
  fakeSandbox,
  type McpServer,
  type MemoryProvider,
  type Model,
  scriptedModel,
  sqlite,
  tool,
} from "../../src";
import { dryPin } from "../../src/agent/dry-pin";
import { openStore } from "../../src/agent/sqlite";
import { ThreadStartedData } from "../../src/log";
import { markTestKit } from "../../src/model/guard";
import { knownEvents } from "../../src/reduce";
import { pinnedLine0 } from "../../src/render/prefix";
import { lookupOrder, say } from "./kit";

// The dry pin drift runs on (spec lane 22, B.3, tests 4a and 4d): no setup, no secret, no MCP
// connection, and for an agent without MCP or setup-bearing extensions the same line 0 and
// config_hash a real run pins after setup.

const counts = { model: 0, extension: 0, connects: 0, memory: 0 };
const counted = (key: keyof typeof counts) => async (): Promise<void> => {
  counts[key] += 1;
};

function withSetup(model: Model): Model {
  const m = { ...model, setup: counted("model") };
  markTestKit(m);
  return m;
}

const refusing: McpServer = {
  kind: "mcp",
  name: "jira",
  connect: async () => {
    counts.connects += 1;
    throw new Error("connection refused");
  },
};

const unbound = async () =>
  ({
    ok: false,
    error: { code: "unavailable", message: "not in tests" },
  }) as const;
const memory: MemoryProvider = {
  remember: unbound,
  recall: unbound,
  forget: unbound,
  setup: counted("memory"),
};

const crm = extension({
  name: "crm",
  instructions: "Look customers up in the CRM.",
  tools: [
    tool({
      name: "crm_lookup",
      description: "Look a customer up.",
      input: z.object({ email: z.string() }),
      effect: "read_only",
      execute: async ({ email }) => email,
    }),
  ],
  setup: counted("extension"),
});

/** The thread_started a real run of `bot` pinned, after setup. */
async function realPin(bot: Agent<undefined, unknown>) {
  const store = sqlite(":memory:");
  const run = await bot.run("Hi", { store });
  const { log } = await openStore(store);
  const read = await log.read(run.thread.branch);
  if (!read.ok) throw new Error(read.error.message);
  const first = knownEvents(read.value)[0];
  if (first?.type !== "thread_started") throw new Error("no thread_started");
  return ThreadStartedData.parse(first.data);
}

describe("dryPin", () => {
  test("runs no setup, reads no secret and opens no MCP connection; it names what it skipped", async () => {
    const bot = agent({
      name: "support",
      model: withSetup(scriptedModel({ responses: [] })),
      tools: [lookupOrder, refusing],
      extensions: [crm],
      memory,
    });
    const dry = dryPin(bot);
    expect(counts).toEqual({ model: 0, extension: 0, connects: 0, memory: 0 });
    expect([dry.mcp, dry.setupExtensions, dry.setupProviders]).toEqual([
      ["jira"],
      ["crm"],
      ["memory"],
    ]);
    const crmTool = dry.started.tools.find((t) => t.name === "crm__crm_lookup");
    expect(crmTool?.origin).toEqual({ extension: "crm" });
  });

  const plain = extension({
    name: "audit",
    instructions: "Log every refund.",
    tools: [
      tool({
        name: "note",
        description: "Write a note.",
        input: z.object({ text: z.string() }),
        effect: "read_only",
        execute: async ({ text }) => text,
      }),
    ],
    hooks: { beforeTool: async () => ({ decision: "allow" }) },
  });
  const reviewer = agent({
    name: "reviewer",
    model: scriptedModel({ responses: [] }),
  });
  const fixtures: readonly [string, () => Agent<undefined, unknown>][] = [
    [
      "a bare agent",
      () => agent({ model: scriptedModel({ responses: [say("hi")] }) }),
    ],
    [
      "tools, a sandbox and a fallback",
      () =>
        agent({
          name: "support",
          instructions: "You handle refunds.",
          model: scriptedModel({ responses: [say("hi")] }),
          fallback: [scriptedModel({ responses: [] })],
          tools: [lookupOrder],
          sandbox: fakeSandbox(),
        }),
    ],
    [
      "an extension without setup, its tools carrying their origin",
      () =>
        agent({
          name: "support",
          model: scriptedModel({ responses: [say("hi")] }),
          extensions: [plain],
        }),
    ],
    [
      "subagents and structured output",
      () =>
        agent({
          name: "lead",
          model: scriptedModel({
            responses: [
              {
                content: [
                  {
                    type: "tool_use",
                    call_id: "f",
                    name: "final_output",
                    input: { ok: true },
                  },
                ],
                stop_reason: "tool_use",
                usage: { input_tokens: 1, output_tokens: 1 },
              },
            ],
          }),
          subagents: [reviewer],
          output: z.object({ ok: z.boolean() }),
        }),
    ],
  ];
  for (const [name, make] of fixtures)
    test(`${name}: the dry pin's line 0 and config_hash equal the real pin's`, async () => {
      const bot = make();
      const dry = dryPin(bot).started;
      const real = await realPin(bot);
      expect(pinnedLine0(dry)).toBe(pinnedLine0(real));
      expect(dry.config_hash).toBe(real.config_hash);
    });
});
