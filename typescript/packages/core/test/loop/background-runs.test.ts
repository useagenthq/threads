import { describe, expect, test } from "bun:test";
import type { KnownEvent, Principal } from "../../src/log";
import type { ChildEnd, Halt, LoopConfig, Subagent } from "../../src/loop";
import { resume } from "../../src/loop";
import type { EventDraft } from "../../src/store";
import { pendingWakes, rebuildWakes } from "../../src/store/wakes";
import { frameworkSpec } from "../../src/tools/framework";
import { ROOT, unwrap } from "../store/helpers";
import { events, harness } from "./harness";

// The writer rule for background wakes (spec/schema/README.md, "Background wakes"): results of
// different runs are never recorded under one woken. Children of two runs that end at one
// boundary give two appends, each with its own woken, in spawn order; and the pending_wakes
// rows follow the log.

const usage = { input_tokens: 10, output_tokens: 2 };
const say = (text: string) => ({
  content: [{ type: "text", text }],
  stop_reason: "end_turn",
  usage,
});
const scan = (id: string, agent = "scanner") => ({
  content: [
    {
      type: "tool_use",
      call_id: id,
      name: "spawn_agent",
      input: { agent, prompt: "Scan.", background: true },
    },
  ],
  stop_reason: "tool_use",
  usage,
});
const alice: Principal = { issuer: "api", tenant: "local", subject: "alice" };
const bob: Principal = { issuer: "api", tenant: "local", subject: "bob" };
const done: ChildEnd = {
  status: "completed",
  output: "Clean.",
  usage: { input_tokens: 1, output_tokens: 1 },
};
const busy: Halt = { code: "branch_busy", message: "held elsewhere" };

const input = (who: Principal, text: string): EventDraft => ({
  type: "user_input",
  type_version: 1,
  critical: true,
  actor: { kind: "user", principal: who },
  data: { source: "api", text },
});

/** A scanner whose runs each wait for the next turn to complete, then give `ends` in turn. */
function scanner(ends: (ChildEnd | Halt)[]): {
  readonly sub: Subagent;
  readonly onEvent: (e: KnownEvent) => void;
} {
  let gate = Promise.withResolvers<void>();
  return {
    sub: {
      run: async () => {
        await gate.promise;
        return ends.shift() ?? busy;
      },
      stop: async () => true,
      held: async () => false,
    },
    onEvent: (e) => {
      if (e.type !== "turn_completed") return;
      gate.resolve();
      gate = Promise.withResolvers<void>();
    },
  };
}

function config(kid: ReturnType<typeof scanner>): Partial<LoopConfig> {
  return {
    agents: {
      name: "lead",
      subagent: () => kid.sub,
      subagents: ["scanner", "licenses"],
    },
    onEvent: kid.onEvent,
  };
}

async function twoRuns(): Promise<readonly KnownEvent[]> {
  const h = await harness(
    [frameworkSpec("spawn_agent")],
    [],
    [
      scan("c1"),
      say("Alice's scan is running."),
      scan("c2", "licenses"),
      say("Bob's scan is running."),
      say("Alice's scan is clean."),
      say("Bob's scan is clean."),
    ],
  );
  // Alice's child can't run the first time: her run halts with it still to report.
  const first = scanner([busy, done, done]);
  const one = unwrap(await h.store.acquire(ROOT, "one", 30_000));
  const end = await resume(one, h.artifacts, h.config(config(first)), {
    input: input(alice, "Scan the dependencies."),
  });
  expect(end.kind).toBe("halted");
  expect(pendingWakes(events(one), ROOT)).toHaveLength(1);
  await one.release();
  // Bob's run relaunches it; both children end once Bob's turn has completed.
  const two = unwrap(await h.store.acquire(ROOT, "two", 30_000));
  const again = await resume(two, h.artifacts, h.config(config(first)), {
    input: input(bob, "Scan the licenses."),
  });
  expect(again.kind).toBe("idle");
  return events(two);
}

describe("background results of two runs at one boundary", () => {
  test("give two appends, each with its own woken, in spawn order", async () => {
    const log = await twoRuns();
    const tail = log
      .slice(log.findIndex((e) => e.type === "agent_finished"))
      .map((e) => e.type);
    expect(tail).toEqual([
      "agent_finished",
      "tool_result_late",
      "woken",
      "model_request",
      "model_response",
      "turn_completed",
      "agent_finished",
      "tool_result_late",
      "woken",
      "model_request",
      "model_response",
      "turn_completed",
    ]);
    const woken = log.flatMap((e) => (e.type === "woken" ? [e] : []));
    expect(woken.map((e) => e.actor.principal)).toEqual([alice, bob]);
    // No late result is left without its wake: each woken names exactly the one before it.
    const late = log.flatMap((e) =>
      e.type === "tool_result_late" ? [e.event_id] : [],
    );
    expect(woken.map((e) => e.data.causes)).toEqual(late.map((id) => [id]));
    expect(pendingWakes(log, ROOT)).toEqual([]);
  });
});

describe("pending_wakes", () => {
  test("the rows follow the log, and an index wipe rebuilds them", async () => {
    const h = await harness(
      [frameworkSpec("spawn_agent")],
      [],
      [scan("c1"), say("Running.")],
    );
    const kid = scanner([busy]);
    const writer = unwrap(await h.store.acquire(ROOT, "one", 30_000));
    await resume(writer, h.artifacts, h.config(config(kid)), {
      input: input(alice, "Scan."),
    });
    const rows = () =>
      h.db.transaction((tx) =>
        tx.all("SELECT branch_id, child_thread_id FROM pending_wakes"),
      );
    const before = await rows();
    expect(before).toEqual(
      pendingWakes(events(writer), ROOT).map((child) => ({
        branch_id: ROOT,
        child_thread_id: child,
      })),
    );
    expect(before).toHaveLength(1);
    await h.db.transaction((tx) => tx.run("DELETE FROM pending_wakes"));
    await h.db.transaction((tx) =>
      rebuildWakes(tx, async (branch) =>
        branch === ROOT ? events(writer) : undefined,
      ),
    );
    expect(await rows()).toEqual(before);
  });
});
