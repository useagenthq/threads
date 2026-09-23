import { describe, expect, test } from "bun:test";
import {
  agent,
  openThread,
  scriptedModel,
  sqlite,
  type ThreadRef,
} from "../../src";
import { openStore } from "../../src/agent/sqlite";
import type { KnownEvent } from "../../src/log";
import { knownEvents } from "../../src/reduce";
import { unwrap } from "../store/helpers";

// todo_write: always pinned; a valid list is recorded as todos_updated
// before its result, a malformed one fails with an error result and appends nothing, and the
// reminder producer shows open items after 10 turns without a write.

const usage = { input_tokens: 10, output_tokens: 2 };
const say = (text: string) => ({
  content: [{ type: "text", text }],
  stop_reason: "end_turn",
  usage,
});
const write = (todos: unknown, id: string) => ({
  content: [
    { type: "tool_use", call_id: id, name: "todo_write", input: { todos } },
  ],
  stop_reason: "tool_use",
  usage,
});
const item = {
  id: "1",
  content: "Add the column",
  status: "in_progress",
} as const;

async function events(
  store: ReturnType<typeof sqlite>,
  thread: ThreadRef,
): Promise<readonly KnownEvent[]> {
  const { log } = await openStore(store);
  return knownEvents(unwrap(log.read(thread.branch)));
}

describe("todo_write", () => {
  test("is pinned with no configuration", async () => {
    const store = sqlite(":memory:");
    const bot = agent({ model: scriptedModel({ responses: [say("hi")] }) });
    const result = await bot.run("hi", { store });
    const started = (await events(store, result.thread)).find(
      (e) => e.type === "thread_started",
    );
    expect(
      started?.type === "thread_started" &&
        started.data.tools.map((t) => [t.name, t.effect_class]),
    ).toEqual([
      ["read_tool_result", "read_only"],
      ["todo_write", "read_only"],
    ]);
  });

  test("records todos_updated before the result; the thread handle reads the latest list", async () => {
    const store = sqlite(":memory:");
    const done = { ...item, status: "completed" };
    const model = scriptedModel({
      responses: [write([item], "c1"), write([done], "c2"), say("done")],
    });
    const result = await agent({ model }).run("plan", { store });
    const log = await events(store, result.thread);
    const types = log.map((e) => e.type);
    const updated = types.indexOf("todos_updated");
    expect(types[updated + 1]).toBe("tool_result");
    const thread = unwrap(await openThread(store, result.thread.id));
    expect<unknown>(await thread.todos()).toEqual([done]);
  });

  test("a malformed list or duplicate ids fail with an error result and append nothing", async () => {
    const store = sqlite(":memory:");
    const model = scriptedModel({
      responses: [
        write([item], "c1"),
        write([{ ...item, status: "done" }], "c2"),
        write([item, { ...item, content: "again" }], "c3"),
        say("ok"),
      ],
    });
    const result = await agent({ model }).run("plan", { store });
    const log = await events(store, result.thread);
    expect(log.filter((e) => e.type === "todos_updated")).toHaveLength(1);
    const errors = log.flatMap((e) =>
      e.type === "tool_result" && e.data.is_error ? [e.data.call_id] : [],
    );
    expect<unknown>(errors).toEqual(["c2", "c3"]);
    const thread = unwrap(await openThread(store, result.thread.id));
    expect(await thread.todos()).toEqual([item]);
  });

  test("open items and 10 turns without todo_write: one reminder, at most once per 10 turns", async () => {
    const store = sqlite(":memory:");
    const responses = [write([item], "c1"), say("Planned.")];
    for (let i = 0; i < 12; i++) responses.push(say("Working on it."));
    const bot = agent({ model: scriptedModel({ responses }) });
    const first = await bot.run("Plan the migration.", { store });
    for (let i = 0; i < 12; i++)
      await bot.run(`Status ${i + 1}?`, { store, thread: first.thread });
    const log = await events(store, first.thread);
    const reminders = log.flatMap((e, i) =>
      e.type === "injected" && e.data.source === "todo"
        ? [[e, log[i - 1]]]
        : [],
    );
    expect(reminders).toHaveLength(1);
    const [note, before] = reminders[0] ?? [];
    expect(note?.type === "injected" && note.data).toEqual({
      source: "todo",
      trust: "untrusted_reference",
      origin: { id: "todos" },
      text: "- [in_progress] Add the column",
    });
    expect(before?.type === "user_input" && before.data.text).toBe(
      "Status 11?",
    );
  });
});
