import { describe, expect, test } from "bun:test";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { z } from "zod";
import {
  agent,
  fakeSandbox,
  openThread,
  type Store,
  scriptedModel,
  sqlite,
  type Thread,
  tool,
} from "../../src";
import { openStore } from "../../src/agent/sqlite";
import { knownEvents } from "../../src/reduce";
import { code, unwrap } from "../store/helpers";

// Thread.fork({mode: "stub"}) (ADR 0010, spec/api.json Thread.fork): the stubs are frozen as an
// artifact on the fork event, so a child reopened by id in a fresh handle still answers every
// mediated operation from what the parent recorded, and later parent appends can't reach it.

/** sha256 of the canonical `{"text":"x"}` the recorded send was called with. */
const ARGS_HASH =
  "fcd1ccec08db6f78a81fee6c26da9e6b8d0d3ba58b4403713fffebcfaa6cf119";

const usage = { input_tokens: 10, output_tokens: 2 };
const say = (text: string) => ({
  content: [{ type: "text", text }],
  stop_reason: "end_turn",
  usage,
});
const use = (name: string, input: Record<string, unknown>, id: string) => ({
  content: [{ type: "tool_use", call_id: id, name, input }],
  stop_reason: "tool_use",
  usage,
});

/** A mediated host tool: sending is an external effect, so it is stubbed, never replayed live. */
function sender(sent: string[]) {
  return tool({
    name: "send",
    description: "Send.",
    input: z.object({ text: z.string() }),
    runs: "host",
    execute: async ({ text }: { text: string }): Promise<string> => {
      sent.push(text);
      return `sent ${text}`;
    },
  });
}

function bot(sent: string[], responses: readonly unknown[]) {
  const sandbox = fakeSandbox();
  return {
    sandbox,
    bot: agent({
      model: scriptedModel({ responses: [...responses] }),
      sandbox,
      tools: [sender(sent)],
      permissions: { mode: "bypass", allow_bypass: true },
    }),
  };
}

/** A thread with a fork point after a file write, then a mediated send in its second turn. */
async function recorded(
  sent: string[],
  store: Store = sqlite(":memory:"),
): Promise<Thread> {
  const first = bot(sent, [
    use("write", { path: "a.txt", content: "A" }, "w1"),
    say("Wrote it."),
  ]);
  const one = await first.bot.run("write", { store });
  expect(one.status === "failed" ? one.error : one.status).toBe("completed");
  const second = bot(sent, [use("send", { text: "x" }, "s1"), say("Sent.")]);
  const two = await second.bot.run("send", { store, thread: one.thread });
  expect(two.status).toBe("completed");
  return unwrap(
    await openThread(store, one.thread.id, { sandbox: first.sandbox }),
  );
}

async function stubChild(thread: Thread): Promise<Thread> {
  const points = await thread.forkPoints();
  const point = points[0];
  if (point === undefined) throw new Error("the parent has a fork point");
  return unwrap(await thread.fork(point, { mode: "stub" }));
}

describe("a stub fork freezes its script", () => {
  test("the fork event records mode and the script's ref", async () => {
    const sent: string[] = [];
    const parent = await recorded(sent);
    const child = await stubChild(parent);
    const { log } = await openStore(child.store);
    const chain = knownEvents(unwrap(await log.read(child.branch)));
    const fork = chain.find((e) => e.type === "fork");
    if (fork?.type !== "fork") throw new Error("the child begins with a fork");
    expect(fork.data.mode).toBe("stub");
    expect(fork.data.stub_script_ref?.media_type).toBe("application/json");
    const bytes = unwrap(
      await (await openStore(child.store)).artifacts.get(
        fork.data.stub_script_ref?.sha256 ?? "",
      ),
    );
    // RFC 8785 canonical JSON: Python's test pins the same bytes for the same parent log.
    expect(new TextDecoder().decode(bytes)).toBe(
      `{"stubs":[{"args_hash":"${ARGS_HASH}","is_error":false,"occurrence":0,"output":"sent x","tool":"send"}]}`,
    );
  });

  test("a parent whose committed output is gone fails the fork and creates no child", async () => {
    const sent: string[] = [];
    const dir = mkdtempSync(join(tmpdir(), "threads-stub-fork-"));
    try {
      const parent = await recorded(sent, sqlite(dir));
      const { log } = await openStore(parent.store);
      const chain = knownEvents(unwrap(await log.read(parent.branch)));
      const commit = chain.find(
        (e) => e.type === "effect_commit" && e.data.call_id === "s1",
      );
      if (commit?.type !== "effect_commit")
        throw new Error("the recorded send committed an output");
      const sha = commit.data.result_ref.sha256;
      rmSync(join(dir, "artifacts", "sha256", sha.slice(0, 2), sha));
      const before = (await parent.branches()).length;
      const points = await parent.forkPoints();
      const point = points[0];
      if (point === undefined) throw new Error("the parent has a fork point");
      const refused = await parent.fork(point, { mode: "stub" });
      expect(code(refused)).toBe("artifact_missing");
      expect((await parent.branches()).length).toBe(before);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  test("a child reopened by id still runs stubbed, and branches() reports stub", async () => {
    const sent: string[] = [];
    const parent = await recorded(sent);
    const child = await stubChild(parent);
    // A handle opened by id knows nothing about the fork that made it.
    const reopened = unwrap(
      await openThread(child.store, child.id, { branchId: child.branch }),
    );
    const modes = new Map(
      (await reopened.branches()).map((b) => [b.branch_id, b.mode]),
    );
    expect(modes.get(child.branch)).toBe("stub");
    expect(modes.get(parent.branch)).toBe("live");
    const again = bot(sent, [use("send", { text: "x" }, "c1"), say("Again.")]);
    const done = await again.bot.run("send", {
      store: child.store,
      thread: reopened,
    });
    expect(done.status).toBe("completed");
    // Answered from the frozen script: the tool body never ran a second time.
    expect(sent).toEqual(["x"]);
  });

  test("an unmatched mediated call fails closed, never live", async () => {
    const sent: string[] = [];
    const parent = await recorded(sent);
    const child = await stubChild(parent);
    const other = bot(sent, [use("send", { text: "y" }, "c1"), say("never")]);
    const refused = await other.bot.run("send", {
      store: child.store,
      thread: child,
    });
    expect(refused.status).toBe("failed");
    expect(refused.status === "failed" ? refused.error.code : "").toBe(
      "unmatched_external_op",
    );
    expect(sent).toEqual(["x"]);
  });

  test("later parent appends never change a child", async () => {
    const sent: string[] = [];
    const parent = await recorded(sent);
    const child = await stubChild(parent);
    const more = bot(sent, [use("send", { text: "z" }, "p2"), say("Sent z.")]);
    expect(
      (await more.bot.run("again", { store: parent.store, thread: parent }))
        .status,
    ).toBe("completed");
    expect(sent).toEqual(["x", "z"]);
    // The child's script was frozen before z: asking for it fails closed.
    const asking = bot(sent, [use("send", { text: "z" }, "c1"), say("never")]);
    const refused = await asking.bot.run("send", {
      store: child.store,
      thread: child,
    });
    expect(refused.status).toBe("failed");
    expect(sent).toEqual(["x", "z"]);
  });

  test("stub mode needs a sandbox that enforces deny-all egress", async () => {
    const sent: string[] = [];
    const parent = await recorded(sent);
    const points = await parent.forkPoints();
    const point = points[0];
    if (point === undefined) throw new Error("the parent has a fork point");
    const open = fakeSandbox();
    const unenforced = {
      ...open,
      info: { ...open.info, egress: "unenforced" as const },
    };
    const handle = unwrap(
      await openThread(parent.store, parent.id, { sandbox: unenforced }),
    );
    expect(code(await handle.fork(point, { mode: "stub" }))).toBe(
      "egress_policy_unsupported",
    );
  });
});
