import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import type { KnownEvent, Principal } from "../../src/log";
import { parseLogLine } from "../../src/log/parse";
import type { ChildEnd, Halt, LoopConfig, Subagent } from "../../src/loop";
import { resume } from "../../src/loop";
import { mayWake } from "../../src/loop/agents/wake";
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
    },
    onEvent: kid.onEvent,
  };
}

async function twoRuns(): Promise<readonly KnownEvent[]> {
  const h = harness(
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
  const one = unwrap(h.store.acquire(ROOT, "one", 30_000));
  const end = await resume(one, h.artifacts, h.config(config(first)), {
    input: input(alice, "Scan the dependencies."),
  });
  expect(end.kind).toBe("halted");
  expect(pendingWakes(events(one), ROOT)).toHaveLength(1);
  one.release();
  // Bob's run relaunches it; both children end once Bob's turn has completed.
  const two = unwrap(h.store.acquire(ROOT, "two", 30_000));
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
    const h = harness(
      [frameworkSpec("spawn_agent")],
      [],
      [scan("c1"), say("Running.")],
    );
    const kid = scanner([busy]);
    const writer = unwrap(h.store.acquire(ROOT, "one", 30_000));
    await resume(writer, h.artifacts, h.config(config(kid)), {
      input: input(alice, "Scan."),
    });
    const rows = () =>
      h.db.all("SELECT branch_id, child_thread_id FROM pending_wakes", []);
    const before = rows();
    expect(before).toEqual(
      pendingWakes(events(writer), ROOT).map((child) => ({
        branch_id: ROOT,
        child_thread_id: child,
      })),
    );
    expect(before).toHaveLength(1);
    h.db.run("DELETE FROM pending_wakes", []);
    rebuildWakes(h.db, (branch) =>
      branch === ROOT ? events(writer) : undefined,
    );
    expect(rows()).toEqual(before);
  });
});

describe("the wake condition", () => {
  test("a log that has ended (member_ended) never wakes", () => {
    const lines = readFileSync(
      new URL(
        "../../../../../spec/conformance/staged/member-ended-then-input-rejected/log.jsonl",
        import.meta.url,
      ),
      "utf8",
    ).split("\n");
    const ended = lines.flatMap((line) => {
      const parsed = line.includes('"member_ended"')
        ? parseLogLine(line)
        : undefined;
      return parsed?.ok === true && parsed.value.kind === "event"
        ? [parsed.value.event]
        : [];
    });
    expect(ended.map((e) => e.type)).toEqual(["member_ended"]);
    const idle = { turnOpen: false, cancelled: false };
    expect(mayWake(idle, [])).toBe(true);
    expect(mayWake(idle, ended)).toBe(false);
    expect(mayWake({ turnOpen: true, cancelled: false }, [])).toBe(false);
    expect(mayWake({ turnOpen: false, cancelled: true }, [])).toBe(false);
  });
});
