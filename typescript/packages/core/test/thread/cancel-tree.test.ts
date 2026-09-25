import { describe, expect, test } from "bun:test";
import type { Principal } from "../../src/log";
import { knownEvents } from "../../src/reduce";
import { keepLease } from "../../src/store";
import { cancelTree } from "../../src/thread/cancel";
import {
  fixture,
  ROOT,
  started,
  THREAD,
  turnCompleted,
  unwrap,
  userInput,
} from "../store/helpers";

// cancelTree's answer is what the stop loop trusts (spec/schema/README.md, "Subagent cancellation
// and parking"): a child counts as barred only once its cancel is appended, and a child that
// has finished (its turn closed, nothing of its own still running) gets no stray cancel.

const operator: Principal = {
  issuer: "api",
  tenant: "local",
  subject: "operator",
};

async function child(turnOpen: boolean) {
  const f = await fixture();
  unwrap(await f.store.createBranch(THREAD, ROOT));
  const writer = unwrap(await f.store.acquire(ROOT, "child", 1_000));
  const drafts = turnOpen
    ? [started, userInput("Scan.")]
    : [started, userInput("Scan."), turnCompleted];
  unwrap(await writer.append(drafts));
  return { f, writer };
}

const cancels = async (
  f: Awaited<ReturnType<typeof fixture>>,
): Promise<number> =>
  knownEvents(unwrap(await f.store.read(ROOT))).filter(
    (e) => e.type === "cancel_requested",
  ).length;

describe("cancelTree", () => {
  test("a child whose running writer lost its lease is not counted as barred", async () => {
    const { f, writer } = await child(true);
    const stop = keepLease(writer);
    try {
      f.clock.now += 60_000;
      unwrap(await f.store.acquire(ROOT, "usurper"));
      expect(await cancelTree(f.store, THREAD, operator)).toBe(false);
      expect(await cancels(f)).toBe(0);
    } finally {
      await stop();
    }
  });

  test("a child that has finished gets no stray cancel, and counts as stopped", async () => {
    const { f, writer } = await child(false);
    await writer.release();
    expect(await cancelTree(f.store, THREAD, operator)).toBe(true);
    expect(await cancels(f)).toBe(0);
  });

  test("a child with an open turn gets its cancel", async () => {
    const { f, writer } = await child(true);
    await writer.release();
    expect(await cancelTree(f.store, THREAD, operator)).toBe(true);
    expect(await cancels(f)).toBe(1);
  });
});
