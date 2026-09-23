import { describe, expect, test } from "bun:test";
import { mkdtempSync, readdirSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { storeOf } from "../../src/agent/sqlite";
import { BranchId, ThreadId } from "../../src/log";
import { type LoopConfig, resume } from "../../src/loop";
import { scriptedModel } from "../../src/model";
import { fakeSandbox } from "../../src/sandbox";
import { openThread, type Thread } from "../../src/thread";
import { forkBranch } from "../../src/thread/fork";
import { caseStore, loadCase as corpus, loadCase } from "../conformance/cases";
import { runAppending } from "../conformance/recover";
import { scriptedTools } from "../conformance/sandbox";
import { caseSchema } from "../conformance/schema";
import { code, unwrap } from "../store/helpers";

// saveCase (spec/api.json, c90b416): a stub case in the conformance layout, valid against
// case.schema.json and replayed by the same runner as the corpus.

const CHILD = BranchId.parse("0192b000-0000-7000-8000-0000000000c1");
const ALICE = { issuer: "api", tenant: "acme", subject: "alice" };

const RESPONSES = {
  responses: [
    {
      content: [
        {
          type: "tool_use",
          call_id: "call_9",
          name: "read_file",
          input: { path: "README.md" },
        },
      ],
      stop_reason: "tool_use",
      usage: { input_tokens: 40, output_tokens: 4 },
    },
    {
      content: [{ type: "text", text: "It is a demo." }],
      stop_reason: "end_turn",
      usage: { input_tokens: 60, output_tokens: 6 },
    },
  ],
};

/** The corpus's snapshot, forked, then one more recorded turn on the child: what a case saves. */
async function recorded(): Promise<Thread> {
  const c = corpus("fork-at-snapshot-ok");
  const f = caseStore(c);
  f.clock.now = c.now;
  unwrap(f.store.importLog(c.log ?? new Uint8Array()));
  const sandbox = fakeSandbox(c.scripts.sandbox);
  const parent = unwrap(
    f.store.read(BranchId.parse("0192b000-0000-7000-8000-000000000001")),
  );
  const point = parent.events.at(-1)?.event.event_id;
  if (point === undefined)
    throw new Error("the corpus log ends with its snapshot");
  unwrap(
    await forkBranch(f.store, sandbox, {
      parent: parent.segments[0]?.header.branch_id ?? CHILD,
      point,
      child: CHILD,
      knowledge: "pinned",
      holderId: "forker",
    }),
  );
  const writer = unwrap(f.store.acquire(CHILD, "runner"));
  const tools = scriptedTools(
    { tools: { read_file: { output: "# demo\n" } } },
    writer.chain.fold.tools,
    () => f.clock.now,
  );
  const model = scriptedModel(RESPONSES);
  const config: LoopConfig = {
    models: () => model,
    tools: tools.tools,
    authorize: () => ({
      decision: "allow",
      source: "policy",
      rule_id: "conformance_allow",
    }),
    clock: { now: () => f.clock.now, sleepUntil: async () => {} },
    principal: ALICE,
    skewMarginMs: 1000,
  };
  await resume(writer, f.artifacts, config, {
    input: {
      type: "user_input",
      type_version: 1,
      critical: true,
      actor: { kind: "user", principal: ALICE },
      data: { source: "api", text: "Read it again." },
    },
  });
  const store = storeOf({ log: f.store, artifacts: f.artifacts });
  return unwrap(
    await openThread(
      store,
      ThreadId.parse(parent.segments[0]?.header.thread_id),
      {
        branchId: CHILD,
      },
    ),
  );
}

const json = (path: string): unknown => JSON.parse(readFileSync(path, "utf8"));

describe("saveCase", () => {
  test("writes a stub case valid against case.schema.json that the runner replays", async () => {
    const thread = await recorded();
    const dir = mkdtempSync(join(tmpdir(), "threads-cases-"));
    const saved = unwrap(
      await thread.saveCase("read-again", {
        expect: {
          must: [{ type: "tool_result", data: { preview: "# demo\n" } }],
          expect: [{ type: "turn_completed" }],
        },
        externalEffects: "stub",
        dir,
      }),
    );
    expect(saved).toEqual({ path: join(dir, "read-again"), portable: true });
    const files = readdirSync(saved.path).toSorted();
    expect(files).toEqual([
      "artifacts",
      "case.json",
      "expected.threads-py.json",
      "expected.threads-ts.json",
      "log.threads-py.jsonl",
      "log.threads-ts.jsonl",
      "model.json",
      "sandbox.json",
      "stubs.json",
    ]);
    const valid = (def: string, file: string) =>
      caseSchema(def).safeParse(json(join(saved.path, file))).success;
    expect(valid("Case", "case.json")).toBe(true);
    expect(valid("Expected", "expected.threads-ts.json")).toBe(true);
    expect(valid("Expected", "expected.threads-py.json")).toBe(true);
    expect(valid("ModelScript", "model.json")).toBe(true);
    expect(valid("StubScript", "stubs.json")).toBe(true);
    expect(valid("SandboxScript", "sandbox.json")).toBe(true);

    // The same runner as the corpus: import, send input.text, replay, check appended and must.
    const c = loadCase("read-again", dir);
    await runAppending(c, c.log ?? new Uint8Array());
  });

  test("a case without an assertion, or with a must the recorded turn never met, is refused", async () => {
    const thread = await recorded();
    const dir = mkdtempSync(join(tmpdir(), "threads-cases-"));
    const save = (must: readonly { readonly type: string }[]) =>
      thread.saveCase("refused", {
        expect: { must },
        externalEffects: "stub",
        dir,
      });
    expect(code(await save([]))).toBe("invalid_request");
    expect(code(await save([{ type: "compacted" }]))).toBe("invalid_request");
  });
});
