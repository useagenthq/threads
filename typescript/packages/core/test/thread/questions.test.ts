import { describe, expect, test } from "bun:test";
import { agent, scriptedModel, sqlite } from "../../src";
import { deleteThread, hostRunner } from "../../src/host";
import { ThreadId } from "../../src/log";
import type { EventDraft } from "../../src/store";
import { suggestedRules } from "../../src/thread/pending";
import { events, say } from "../agent/wake-kit";
import { type Fixture, unwrap } from "../store/helpers";
import { replayCase } from "./replay";

// spec/schema/README.md, "Questions and remembered rules": only the principal whose input opened
// the asking turn answers; a list answer joins with "\n"; bash suggestions follow shlex.

const alice = { issuer: "api", tenant: "acme", subject: "alice" };
const bob = { issuer: "api", tenant: "acme", subject: "bob" };

/** The corpus case's log through its `parked` (seq 7): alice's question, still open. */
const asked = () => replayCase("ask-user-parks-then-answered", 7);

describe("ask_user answers", () => {
  test("only the principal whose input opened the turn answers", async () => {
    const { thread, events } = await asked();
    const before = events().length;
    expect(await thread.answer("call_1", "Staging.", bob)).toMatchObject({
      ok: false,
      error: { code: "forbidden" },
    });
    expect(events()).toHaveLength(before);
    unwrap(await thread.answer("call_1", "Staging.", alice));
  });

  test("a multi-choice answer is joined with newlines", async () => {
    const { thread, events } = await asked();
    unwrap(await thread.answer("call_1", ["staging", "eu, west"], alice));
    const answered = events().find((e) => e.type === "tool_result");
    expect(answered?.type === "tool_result" && answered.data.preview).toBe(
      "staging\neu, west",
    );
  });
});

/** The case's question on ["red", "blue"] (rule 48), parked at seq 7. */
const colors = () =>
  replayCase("ask-user-rejected-answer-keeps-question-open", 7);

const rows = (f: Fixture) =>
  f.db.all("SELECT call_id, state FROM questions", []);

describe("strict answers and the questions table", () => {
  test("a non-option is invalid_answer and appends nothing; a number picks its option", async () => {
    const { thread, events } = await colors();
    const before = events().length;
    expect(await thread.answer("call_1", "green", alice)).toMatchObject({
      ok: false,
      error: { code: "invalid_answer", message: "answer one of: red, blue" },
    });
    expect(events()).toHaveLength(before);
    unwrap(await thread.answer("call_1", "2", alice));
    const answered = events().find((e) => e.type === "tool_result");
    expect(answered?.type === "tool_result" && answered.data.preview).toBe(
      "blue",
    );
  });

  test("the park opens the question's row and the answer decides it", async () => {
    const { f, thread } = await colors();
    expect(rows(f)).toEqual([{ call_id: "call_1", state: "open" }]);
    unwrap(await thread.answer("call_1", "red", alice));
    expect(rows(f)).toEqual([{ call_id: "call_1", state: "answered" }]);
  });

  test("a missing row blocks nothing: the log decides", async () => {
    const { f, thread } = await colors();
    f.db.run("DELETE FROM questions", []);
    unwrap(await thread.answer("call_1", "red", alice));
  });

  test("deleting the thread deletes its question rows", async () => {
    const { f, threadId } = await colors();
    unwrap(deleteThread(f.db, "acme", ThreadId.parse(threadId), f.clock.now));
    expect(rows(f)).toEqual([]);
  });
});

describe("offering", () => {
  const tools = (started: EventDraft) =>
    started.type === "thread_started"
      ? started.data.tools.map((t) => t.name)
      : [];

  test("only a host run for someone who can answer pins ask_user; agent.run never does", async () => {
    const a = agent({
      name: "demo",
      model: scriptedModel({ responses: [say("Hi.")] }),
    });
    const runner = hostRunner(a);
    if (runner === undefined) throw new Error("agent() registers a runner");
    expect(tools((await runner.started({ answerer: true })).event)).toContain(
      "ask_user",
    );
    expect(tools((await runner.started()).event)).not.toContain("ask_user");
    const store = sqlite(":memory:");
    const ran = await a.run("Hello.", { store });
    const pinned = (await events(store, ran.thread)).find(
      (e) => e.type === "thread_started",
    );
    expect(
      pinned?.type === "thread_started" &&
        pinned.data.tools.some((t) => t.name === "ask_user"),
    ).toBe(false);
  });
});

describe("suggested rules", () => {
  test.each([
    [
      "git push origin main",
      ["bash(git push origin main)", "bash(git push:*)"],
    ],
    ["ls", ["bash(ls)", "bash(ls:*)"]],
    [
      `git commit -m "a b"`,
      [`bash(git commit -m "a b")`, "bash(git commit:*)"],
    ],
    [`"my tool" run x`, [`bash("my tool" run x)`, "bash(my tool run:*)"]],
    [`a\\ b c`, [`bash(a\\ b c)`, "bash(a b c:*)"]],
    [`'it''s' go`, [`bash('it''s' go)`, "bash(its go:*)"]],
    [`echo "unclosed`, [`bash(echo "unclosed)`]],
    // bash(*) allows every command: only configured policy may hold it.
    ["*", []],
  ])("bash %j", (command, rules) => {
    expect(suggestedRules("bash", { command })).toEqual(rules);
  });

  test("a blank bash command or any other tool suggests the tool name", () => {
    expect(suggestedRules("bash", { command: "  " })).toEqual(["bash"]);
    expect(suggestedRules("send_email", { to: "bob" })).toEqual(["send_email"]);
  });
});
