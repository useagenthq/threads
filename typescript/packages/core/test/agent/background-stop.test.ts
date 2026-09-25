import { describe, expect, test } from "bun:test";
import {
  agent,
  type Model,
  openThread,
  scriptedModel,
  sqlite,
} from "../../src";
import { openStore } from "../../src/agent/sqlite";
import type { KnownEvent } from "../../src/log";
import { markTestKit } from "../../src/model/guard";
import { knownEvents } from "../../src/reduce";
import { unwrap } from "../store/helpers";
import {
  alice,
  events,
  gated,
  lead,
  say,
  scan,
  wakes,
  watched,
} from "./wake-kit";

// A stop reaches the whole tree (spec/schema/README.md, "Subagent cancellation and parking" and
// "Run completion"): a tree cancel or an early end bars a subagent that is idle while it waits on
// its own background subagents, so it never wakes; a cancel during the early-end stop ends the
// wait; and a run right after a cancel adopts the subagent still finishing its step.

const TOO_LONG = { error: { reason: "prompt_too_long", http_status: 400 } };

type Store = ReturnType<typeof sqlite>;

/** The log of the first background child the given log spawned. */
async function childLog(
  store: Store,
  parent: readonly KnownEvent[],
): Promise<readonly KnownEvent[]> {
  const spawned = parent.find((e) => e.type === "agent_spawned");
  if (spawned?.type !== "agent_spawned") return [];
  const { log } = await openStore(store);
  const branch = await log.mainBranch(spawned.data.child_thread_id);
  return branch.ok ? knownEvents(unwrap(await log.read(branch.value))) : [];
}

async function until(probe: () => Promise<boolean>): Promise<void> {
  for (let i = 0; i < 400; i++) {
    if (await probe()) return;
    const { promise, resolve } = Promise.withResolvers<void>();
    setTimeout(resolve, 5);
    await promise;
  }
  throw new Error("never got there");
}

/** lead -> mid (background) -> grand (background, gated): mid answers, then waits on grand. */
function tree(open: Promise<void>, leadScript: readonly unknown[]) {
  const grand = agent({
    name: "grand",
    model: gated([say("Grand done.")], open),
  });
  const mid = agent({
    name: "mid",
    model: scriptedModel({
      responses: [scan("m1", "grand"), say("Mid started."), say("Mid woke.")],
    }),
    subagents: [grand],
  });
  return lead(mid, leadScript);
}

describe("a stop reaches an idle subagent that waits on its own", () => {
  test("a tree cancel: the middle subagent never wakes", async () => {
    const store = sqlite(":memory:");
    const gate = Promise.withResolvers<void>();
    const running = tree(gate.promise, [
      scan("c1", "mid"),
      say("Lead started."),
      say("never"),
    ]).stream("Go.", { store, principal: alice });
    for await (const item of running)
      if (item.kind === "event" && item.event.type === "turn_completed") {
        const leadLog = await events(store, {
          id: item.event.thread_id,
          branch: item.event.branch_id,
          store,
        });
        await until(async () =>
          (await childLog(store, leadLog)).some(
            (e) => e.type === "turn_completed",
          ),
        );
        const thread = unwrap(await openThread(store, item.event.thread_id));
        unwrap(await thread.cancel(alice));
      }
    const result = await running.result;
    expect(result.status).toBe("cancelled");
    gate.resolve();
    const mid = await childLog(store, await events(store, result.thread));
    await until(async () =>
      (await childLog(store, mid)).some((e) => e.type === "turn_completed"),
    );
    const after = await childLog(store, await events(store, result.thread));
    expect(wakes(after)).toEqual([]);
    expect(after.some((e) => e.type === "cancel_requested")).toBe(true);
  });

  test("an early end: run() returns with the grandchild still held, and mid never wakes", async () => {
    const store = sqlite(":memory:");
    const gate = Promise.withResolvers<void>();
    const running = tree(gate.promise, [
      scan("c1", "mid"),
      TOO_LONG,
      say("never"),
    ]).stream("Go.", { store, principal: alice });
    for await (const _item of running);
    const result = await running.result;
    expect(result.status).toBe("failed");
    const mid = await childLog(store, await events(store, result.thread));
    expect(wakes(mid)).toEqual([]);
    expect(mid.some((e) => e.type === "cancel_requested")).toBe(true);
    gate.resolve();
  });
});

describe("a cancel during the early-end stop", () => {
  test("ends the wait even while a child hangs", async () => {
    const store = sqlite(":memory:");
    const gate = Promise.withResolvers<void>();
    const kid = watched(gated([say("Clean.")], gate.promise));
    const scanner = agent({ name: "scanner", model: kid.model });
    // The lead fails only once the child is inside its model call, where it hangs.
    const script = scriptedModel({ responses: [scan("c1"), TOO_LONG] });
    let asked = 0;
    const leadModel: Model = {
      ...script,
      send: async function* (...args: Parameters<Model["send"]>) {
        asked += 1;
        if (asked === 2) await kid.entered;
        yield* script.send(...args);
      },
    };
    markTestKit(leadModel);
    const bot = agent({ name: "lead", model: leadModel, subagents: [scanner] });
    const running = bot.stream("Scan.", { store, principal: alice });
    for await (const item of running)
      if (item.kind === "event" && item.event.type === "turn_completed") {
        const thread = unwrap(await openThread(store, item.event.thread_id));
        unwrap(await thread.cancel(alice));
      }
    const result = await running.result;
    gate.resolve();
    expect(result.status).toBe("failed");
  });
});

describe("a run right after a cancel", () => {
  test("adopts the subagent still finishing its step and succeeds", async () => {
    const store = sqlite(":memory:");
    const gate = Promise.withResolvers<void>();
    const kid = watched(gated([say("Clean.")], gate.promise));
    const scanner = agent({ name: "scanner", model: kid.model });
    const running = lead(scanner, [
      scan("c1"),
      say("Started."),
      say("x"),
    ]).stream("Scan.", { store, principal: alice });
    for await (const item of running)
      if (item.kind === "event" && item.event.type === "turn_completed") {
        await kid.entered;
        const thread = unwrap(await openThread(store, item.event.thread_id));
        unwrap(await thread.cancel(alice));
      }
    const first = await running.result;
    expect(first.status).toBe("cancelled");
    setTimeout(gate.resolve, 20);
    const second = await lead(scanner, [say("Second.")]).run("Again.", {
      store,
      principal: alice,
      thread: first.thread,
    });
    expect(second).toMatchObject({ status: "completed", output: "Second." });
    expect(kid.calls()).toBe(1);
    const log = await events(store, first.thread);
    expect(log.filter((e) => e.type === "agent_finished")).toHaveLength(1);
    expect(wakes(log)).toEqual([]);
  });
});
