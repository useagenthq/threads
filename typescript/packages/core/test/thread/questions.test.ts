import { describe, expect, test } from "bun:test";
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { z } from "zod";
import { openThread } from "../../src";
import { storeOf } from "../../src/agent/sqlite";
import { BranchId, KnownEvent, ThreadId } from "../../src/log";
import { knownEvents } from "../../src/reduce";
import { suggestedRules } from "../../src/thread/pending";
import { fixture, unwrap } from "../store/helpers";

// spec/schema/README.md, "Questions and remembered rules": only the principal whose input opened
// the asking turn answers; a list answer joins with "\n"; bash suggestions follow shlex.

const alice = { issuer: "api", tenant: "acme", subject: "alice" };
const bob = { issuer: "api", tenant: "acme", subject: "bob" };
const CASE = join(
  import.meta.dir,
  "../../../../../spec/conformance/cases/ask-user-parks-then-answered",
);
const Header = z.object({ thread_id: ThreadId, branch_id: BranchId });

/** The corpus case's log through its `parked` (seq 7): alice's question, still open. */
async function asked() {
  const f = fixture("acme");
  for (const name of readdirSync(join(CASE, "artifacts")))
    f.artifacts.put(readFileSync(join(CASE, "artifacts", name)));
  const lines = readFileSync(join(CASE, "log.jsonl"), "utf8")
    .split("\n")
    .slice(0, 8);
  const header = Header.parse(JSON.parse(lines[0] ?? ""));
  // The case is a Python-written branch; re-append its events through this writer, with the
  // event ids they reference mapped to the new ones.
  unwrap(f.store.createBranch(header.thread_id, header.branch_id));
  const writer = unwrap(f.store.acquire(header.branch_id, "setup"));
  const ids = new Map<string, string>();
  for (let line of lines.slice(1)) {
    for (const [from, to] of ids) line = line.replaceAll(from, to);
    const {
      seq: _seq,
      event_id: old,
      thread_id: _thread,
      branch_id: _branch,
      epoch: _epoch,
      time: _time,
      prev_hash: _prev,
      ...draft
    } = KnownEvent.parse(JSON.parse(line));
    const [added] = unwrap(writer.append([draft]));
    if (added?.kind === "event") ids.set(old, added.event.event_id);
  }
  writer.release();
  const store = storeOf({ log: f.store, artifacts: f.artifacts });
  const thread = unwrap(await openThread(store, header.thread_id));
  const events = () => knownEvents(unwrap(f.store.read(header.branch_id)));
  return { thread, events };
}

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
    ["*", ["bash(*:*)"]],
  ])("bash %j", (command, rules) => {
    expect(suggestedRules("bash", { command })).toEqual(rules);
  });

  test("a blank bash command or any other tool suggests the tool name", () => {
    expect(suggestedRules("bash", { command: "  " })).toEqual(["bash"]);
    expect(suggestedRules("send_email", { to: "bob" })).toEqual(["send_email"]);
  });
});
