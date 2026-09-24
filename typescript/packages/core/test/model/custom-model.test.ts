import { describe, expect, test } from "bun:test";
import { existsSync, mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  agent,
  ConfigError,
  fakeSandbox,
  type Model,
  type Sandbox,
  scriptedModel,
  sqlite,
} from "../../src";
import { openStore } from "../../src/agent/sqlite";
import { markTestKit } from "../../src/model/guard";
import { reduce } from "../../src/reduce";
import { unwrap } from "../store/helpers";

// A custom model is info plus send (spec/api.json Model); lookup is optional. A model or
// sandbox that declares a lookup capability without its method is refused by check() and the
// first run (capability_missing), never by agent(), which stays pure.

const usage = { input_tokens: 1, output_tokens: 1 };

/** A model with no lookup: it answers every request with "hello". */
function hello(lookup: Model["info"]["lookup"] = "none"): Model {
  const inner = scriptedModel({ responses: [] });
  const model: Model = {
    info: { ...inner.info, lookup },
    send: async function* () {
      yield { kind: "part", part: { type: "text", text: "hello" } };
      yield { kind: "done", stop_reason: "end_turn", usage };
    },
  };
  // Never leaves the process; the guard only knows test-kit models.
  markTestKit(model);
  return model;
}

describe("a custom model", () => {
  test("with only info and send runs a turn", async () => {
    const result = await agent({ model: hello() }).run("hi", {
      store: sqlite(":memory:"),
    });
    expect(result.status).toBe("completed");
    expect(result.status === "completed" && result.output).toBe("hello");
  });

  test("declaring lookup without the method: check() refuses, the run throws and writes nothing", async () => {
    const bot = agent({ model: hello("final") });
    expect(await bot.check()).toEqual({
      ok: false,
      error: {
        code: "capability_missing",
        message:
          'model scripted/scripted-1 declares lookup "final" but has no lookup method: implement lookup, or declare lookup: "none"',
      },
    });
    const dir = join(mkdtempSync(join(tmpdir(), "threads-")), "store");
    const run = bot.run("hi", { store: sqlite(dir) });
    await expect(run).rejects.toBeInstanceOf(ConfigError);
    await expect(run).rejects.toMatchObject({ code: "capability_missing" });
    expect(existsSync(dir)).toBe(false);
  });

  test("the same holds for a fallback, a subagent's and a handoff target's model", async () => {
    const fine = scriptedModel({ responses: [] });
    const helper = agent({ name: "helper", model: hello("nonfinal") });
    for (const bot of [
      agent({ model: fine, fallback: [hello("final")] }),
      agent({ model: fine, subagents: [helper] }),
      agent({ model: fine, handoffs: [helper] }),
    ]) {
      const checked = await bot.check();
      expect(checked.ok === false && checked.error.code).toBe(
        "capability_missing",
      );
    }
  });
});

describe("a custom sandbox", () => {
  const check = async (sandbox: Sandbox) =>
    agent({ model: scriptedModel({ responses: [] }), sandbox }).check();
  const declaring = (
    create: Sandbox["info"]["lookup"]["create"],
    snapshot: Sandbox["info"]["lookup"]["snapshot"],
  ) => {
    const {
      lookup: _create,
      lookupSnapshot: _snapshot,
      ...rest
    } = fakeSandbox();
    return { ...rest, info: { ...rest.info, lookup: { create, snapshot } } };
  };

  test("with neither lookup runs a tool and ends the turn at a verified fork point", async () => {
    const write = {
      content: [
        {
          type: "tool_use",
          call_id: "c1",
          name: "write",
          input: { path: "a.txt", content: "A" },
        },
      ],
      stop_reason: "tool_use",
      usage,
    };
    const done = {
      content: [{ type: "text", text: "Done." }],
      stop_reason: "end_turn",
      usage,
    };
    const result = await agent({
      model: scriptedModel({ responses: [write, done] }),
      sandbox: declaring("none", "none"),
      permissions: { mode: "accept_edits" },
    }).run("go", { store: sqlite(":memory:") });
    expect(result.status).toBe("completed");
    const { log } = await openStore(result.thread.store);
    const chain = unwrap(log.read(result.thread.branch));
    expect(reduce(chain, Date.now()).fork_points.length).toBe(1);
  });

  test("needs each lookup method it declares, and only those", async () => {
    expect(await check(declaring("none", "none"))).toEqual({
      ok: true,
      value: undefined,
    });
    const { lookupSnapshot: _snapshot, ...createOnly } = fakeSandbox();
    const create = { create: "final", snapshot: "none" } as const;
    expect(
      await check({
        ...createOnly,
        info: { ...createOnly.info, lookup: create },
      }),
    ).toEqual({ ok: true, value: undefined });
    expect(await check(declaring("final", "none"))).toEqual({
      ok: false,
      error: {
        code: "capability_missing",
        message:
          'sandbox fake declares lookup.create "final" but has no lookup method: implement lookup, or declare lookup.create: "none"',
      },
    });
    expect(await check(declaring("none", "nonfinal"))).toEqual({
      ok: false,
      error: {
        code: "capability_missing",
        message:
          'sandbox fake declares lookup.snapshot "nonfinal" but has no lookupSnapshot method: implement lookupSnapshot, or declare lookup.snapshot: "none"',
      },
    });
  });
});
