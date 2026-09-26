import { describe, expect, test } from "bun:test";
import type { KnownEvent } from "../../src/log";
import { type LoopConfig, resume } from "../../src/loop";
import type { Model } from "../../src/model";
import { markTestKit } from "../../src/model/guard";
import { LogStore, type StoreDriver } from "../../src/store";
import { controlItemsPending } from "../../src/thread/control-items";
import { controls } from "../../src/thread/controls";
import { events, type Harness, harness, userInput } from "../loop/harness";
import { ROOT, THREAD, unwrap } from "../store/helpers";
import { Crash, crashing, mentions } from "../team/crash-kit";
import {
  crashAfterInsert,
  expireLeases,
  inboxRows,
  OPERATOR,
} from "./control-item-kit";

// Thread.cancel across processes (lane 29F). Another process's lease is no longer branch_busy:
// the cancel becomes a durable `api` control item, and the branch's holder applies it at its
// next step boundary, exactly once, before anything it would dispatch next.

const say = (text: string) => ({
  content: [{ type: "text", text }],
  stop_reason: "end_turn",
  usage: { input_tokens: 5, output_tokens: 5 },
});

type Pair = {
  readonly h: Harness;
  /** A second process over the same database. */
  readonly other: LogStore;
  readonly config: (o?: Partial<LoopConfig>) => LoopConfig;
  readonly over: (db: StoreDriver) => Promise<LogStore>;
};

/** `open`: the branch already has a turn in flight, which a cancel is meant to stop. */
async function pair(open = false): Promise<Pair> {
  const h = await harness([], open ? [userInput("first")] : [], [
    say("Done."),
    say("Done."),
  ]);
  const over = async (db: StoreDriver): Promise<LogStore> =>
    unwrap(
      await LogStore.open(db, () => h.clock.now, h.artifacts, h.store.tenant),
    );
  const config = (o: Partial<LoopConfig> = {}): LoopConfig =>
    h.config({ controlItems: () => controlItemsPending(h.db, THREAD), ...o });
  return { h, other: await over(h.db), config, over };
}

const types = (log: readonly KnownEvent[]): readonly string[] =>
  log.map((e) => e.type);

/** The event types after the first cancel_requested; a dispatch among them breaks invariant 2. */
function afterBarrier(log: readonly KnownEvent[]): readonly string[] {
  const at = types(log).indexOf("cancel_requested");
  expect(at).toBeGreaterThanOrEqual(0);
  return types(log).slice(at + 1);
}

const barriers = (log: readonly KnownEvent[]): number =>
  types(log).filter((t) => t === "cancel_requested").length;

describe("Thread.cancel writes an inbox control item", () => {
  test("a lease held elsewhere writes one durable item, not branch_busy", async () => {
    const { h, other } = await pair();
    const holder = unwrap(await h.store.acquire(ROOT, "holder"));
    try {
      const done = await controls(other, THREAD, ROOT).cancel(OPERATOR);
      expect(done.ok && "item_key" in done.value).toBe(true);
      const rows = await inboxRows(h.db);
      expect(rows).toHaveLength(1);
      expect(rows[0]?.channel).toBe("api");
      expect(rows[0]?.consumed_seq).toBeNull();
      // The request appends nothing: the log records the cancel where it is applied.
      expect(types(events(holder))).not.toContain("cancel_requested");
    } finally {
      await holder.release();
    }
  });

  test("a principal of another tenant is forbidden and writes nothing", async () => {
    const { h, other } = await pair();
    const holder = unwrap(await h.store.acquire(ROOT, "holder"));
    try {
      const done = await controls(other, THREAD, ROOT).cancel({
        issuer: "api",
        tenant: "other",
        subject: "mallory",
      });
      expect(done.ok ? "ok" : done.error.code).toBe("forbidden");
      expect(await inboxRows(h.db)).toHaveLength(0);
    } finally {
      await holder.release();
    }
  });

  test("a free branch still appends its barrier, with no item", async () => {
    const { h, other } = await pair();
    const done = await controls(other, THREAD, ROOT).cancel(OPERATOR);
    expect(done.ok && "event_id" in done.value).toBe(true);
    expect(await inboxRows(h.db)).toHaveLength(0);
  });
});

describe("the holder applies it once, at a step boundary", () => {
  test("a kill after the item's insert leaves one item, applied once", async () => {
    const { h, config, over } = await pair();
    const dying = await over(crashAfterInsert(h.db));
    const holder = unwrap(await h.store.acquire(ROOT, "holder"));
    try {
      await expect(
        controls(dying, THREAD, ROOT).cancel(OPERATOR),
      ).rejects.toThrow(Crash);
      const pending = await inboxRows(h.db);
      expect(pending).toHaveLength(1);
      expect(pending[0]?.consumed_seq).toBeNull();
    } finally {
      await holder.release();
    }
    const writer = unwrap(await h.store.acquire(ROOT, "run"));
    await resume(writer, h.artifacts, config(), { input: userInput("go") });
    expect(barriers(events(writer))).toBe(1);
    const rows = await inboxRows(h.db);
    expect(rows).toHaveLength(1);
    expect(rows[0]?.consumed_seq).toBeGreaterThan(0);
    // Applying again finds nothing: the CAS consumed it in the barrier's own transaction.
    expect(await controlItemsPending(h.db, THREAD)).toBe(false);
  });

  test("the next lease taker applies it first, before any dispatch", async () => {
    const { h, other, config } = await pair(true);
    // The item is written while the holder has the branch, then that process dies: its lease
    // goes, the item stays, and the turn it was meant to stop is still open.
    const dead = unwrap(await h.store.acquire(ROOT, "gone"));
    const done = await controls(other, THREAD, ROOT).cancel(OPERATOR);
    expect(done.ok && "item_key" in done.value).toBe(true);
    await dead.release();
    const writer = unwrap(await h.store.acquire(ROOT, "next"));
    await resume(writer, h.artifacts, config());
    const log = events(writer);
    expect(types(log)).not.toContain("model_request");
    expect(afterBarrier(log)).not.toContain("model_request");
    expect(types(log)).toContain("cancelled");
    // Nothing was sent: the barrier was the run's first step.
    expect(h.model.remaining()).toBe(2);
  });

  test("an item from another host during an in-flight call stops the next dispatch", async () => {
    const { h, other, config } = await pair();
    const inner = h.model;
    let sends = 0;
    const model: Model = {
      info: inner.info,
      send: (request, context, options) => {
        sends += 1;
        const sending = inner.send(request, context, options);
        return (async function* () {
          if (sends === 1) {
            // Host B, while host A's call is in flight: durable, applied at A's next boundary.
            const done = await controls(other, THREAD, ROOT).cancel(OPERATOR);
            expect(done.ok && "item_key" in done.value).toBe(true);
          }
          yield* sending;
        })();
      },
    };
    markTestKit(model);
    const writer = unwrap(await h.store.acquire(ROOT, "holder"));
    await resume(writer, h.artifacts, config({ models: () => model }), {
      input: userInput("go"),
    });
    const log = events(writer);
    expect(sends).toBe(1);
    expect(afterBarrier(log)).not.toContain("model_request");
    expect(types(log)).toContain("cancelled");
    expect(barriers(log)).toBe(1);
    const rows = await inboxRows(h.db);
    expect(rows).toHaveLength(1);
    expect(rows[0]?.consumed_seq).toBeGreaterThan(0);
  });

  test("a holder killed right after applying dispatches nothing on the restart", async () => {
    const { h, other, config, over } = await pair(true);
    const dyingDb = crashing(h.db, {
      name: "the turn's end after the barrier",
      at: (sql, params) =>
        sql.includes("INSERT INTO events") &&
        mentions(params, "turn_completed"),
    });
    const dying = await over(dyingDb);
    const holder = unwrap(await dying.acquire(ROOT, "holder"));
    const done = await controls(other, THREAD, ROOT).cancel(OPERATOR);
    expect(done.ok && "item_key" in done.value).toBe(true);
    // The holder applies it at its boundary, then dies before the turn's own end is durable.
    await expect(resume(holder, h.artifacts, config())).rejects.toThrow(Crash);
    const applied = await inboxRows(h.db);
    expect(applied[0]?.consumed_seq).toBeGreaterThan(0);
    // The restart finishes the cancellation and dispatches nothing after the barrier.
    await expireLeases(h.db);
    const next = unwrap(await h.store.acquire(ROOT, "restart"));
    await resume(next, h.artifacts, config());
    const log = events(next);
    expect(barriers(log)).toBe(1);
    expect(afterBarrier(log)).not.toContain("model_request");
    expect(types(log)).toContain("cancelled");
    expect(h.model.remaining()).toBe(2);
  });
});
