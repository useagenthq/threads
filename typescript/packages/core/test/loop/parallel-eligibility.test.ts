import { describe, expect, test } from "bun:test";
import { z } from "zod";
import { agent, extension, scriptedModel, sqlite, tool } from "../../src";
import { resume } from "../../src/loop";
import { resumed } from "../../src/thread/control";
import { ROOT, unwrap } from "../store/helpers";
import { events } from "./harness";
import { after, calls, done, FINAL, impl, read, run } from "./parallel-kit";

// Who joins a group (spec/schema/README.md, Parallel tool calls): app and extension tools
// declared concurrent do; framework and built-in tools and an approved ask run alone.

const usage = { input_tokens: 10, output_tokens: 2 };
const ALICE = { issuer: "api", tenant: "acme", subject: "alice" };

describe("eligibility on the bound tools", () => {
  test("an extension tool joins a group; todo_write and read_tool_result run alone", async () => {
    const trace: string[] = [];
    const traced = (name: string) =>
      tool({
        name,
        description: `The ${name} tool.`,
        input: z.object({}),
        effect: "read_only",
        concurrent: true,
        execute: async () => {
          trace.push(`start ${name}`);
          await after(5);
          trace.push(`end ${name}`);
          return name;
        },
      });
    const todos = { todos: [{ id: "1", content: "Read", status: "pending" }] };
    const past = { call_id: "call_1", offset: 0, length: 1 };
    const response = {
      content: [
        { type: "tool_use", call_id: "call_1", name: "a", input: {} },
        {
          type: "tool_use",
          call_id: "call_2",
          name: "todo_write",
          input: todos,
        },
        { type: "tool_use", call_id: "call_3", name: "b", input: {} },
        { type: "tool_use", call_id: "call_4", name: "ext__c", input: {} },
        {
          type: "tool_use",
          call_id: "call_5",
          name: "read_tool_result",
          input: past,
        },
        { type: "tool_use", call_id: "call_6", name: "d", input: {} },
      ],
      stop_reason: "tool_use",
      usage,
    };
    const bot = agent({
      model: scriptedModel({ responses: [response, FINAL] }),
      tools: [traced("a"), traced("b"), traced("d")],
      extensions: [extension({ name: "ext", tools: [traced("c")] })],
    });
    const result = await bot.run("go", { store: sqlite(":memory:") });
    expect(result.status).toBe("completed");
    const at = (step: string) => trace.indexOf(step);
    // todo_write (framework) is a barrier: a ends before b starts.
    expect(at("end a")).toBeLessThan(at("start b"));
    // The extension tool joins b's group: it starts before b ends.
    expect(at("start c")).toBeLessThan(at("end b"));
    // read_tool_result (built-in) is a barrier: d starts after b and c end.
    expect(Math.max(at("end b"), at("end c"))).toBeLessThan(at("start d"));
  });

  test("an approved ask still runs alone; the reads after it group up", async () => {
    const trace: string[] = [];
    const body = (name: string) => async () => {
      trace.push(`start ${name}`);
      await after(1);
      trace.push(`end ${name}`);
      return done(name);
    };
    const names = ["a", "b", "c", "d"];
    const first = await run(
      names.map((n) => impl(read(n), body(n))),
      [calls(...names), FINAL],
      () => ({
        authorize: (call) => ({
          decision: call.data.call_id === "call_2" ? "ask" : "allow",
          source: "policy",
        }),
      }),
    );
    expect(first.end.kind).toBe("parked");
    first.h.clock.now += 60_000; // the parked run's lease has lapsed
    const writer = unwrap(
      await first.h.store.acquire(ROOT, "approver", 30_000),
    );
    const asked = events(writer).find((e) => e.type === "approval_requested");
    if (asked?.type !== "approval_requested") throw new Error("one challenge");
    const { challenge_id, call_id, args_hash } = asked.data;
    unwrap(
      await writer.append([
        {
          type: "approval_granted",
          type_version: 1,
          critical: true,
          actor: { kind: "approver", principal: ALICE },
          data: { challenge_id, call_id, args_hash },
        },
      ]),
    );
    const granted = events(writer).at(-1);
    if (granted === undefined) throw new Error("just appended");
    unwrap(
      await writer.append([
        resumed({ kind: "approval", id: challenge_id }, granted.event_id),
      ]),
    );
    const again = await resume(
      writer,
      first.h.artifacts,
      first.h.config({
        tools: new Map(names.map((n) => [n, impl(read(n), body(n))])),
      }),
    );
    expect(again.kind).toBe("idle");
    expect(trace.slice(0, 6)).toEqual([
      "start a",
      "end a",
      "start b",
      "end b",
      "start c",
      "start d",
    ]);
  });
});
