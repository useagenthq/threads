import { expect, test } from "bun:test";
import { resume } from "../../../core/src/loop";
import { events } from "../../../core/test/loop/harness";
import { ROOT, unwrap, userInput } from "../../../core/test/store/helpers";
import { type Drill, drill, types } from "./drill";
import { partner } from "./partner";

// Dispatch authority at the A2A send point (invariant 2). The loop's fence before a dispatch cannot
// cover what happens next: the guard resolves the partner's name, and the branch can change owner in
// that window. So the run's own lease fence rides on the request and is awaited again immediately
// before the transport writes, and a request that arrives there without one is refused rather than
// assumed to be authorized.
//
// Each drill counts what the partner was asked to write, because a drill that could not say how many
// times we sent would pass just as well on an empty log.
//
// Mirrors python/tests/a2a/test_outbound_fence.py.

const ASK = userInput("ask the desk");

/** Another owner takes the branch: past this writer's lease and at a new epoch. */
function usurps(h: Drill): () => Promise<void> {
  return async () => {
    h.clock.now += 60_000;
    unwrap(await h.store.acquire(ROOT, "usurper", 1));
  };
}

test("a send whose lease is taken while the name resolves writes nothing", async () => {
  const p = partner();
  const h = await drill(p);
  p.send = () => {
    throw new Error("this drill never reaches a send");
  };
  const take = usurps(h);
  // The card is read first; the next name we resolve is the send's own.
  p.onResolve = async () => {
    if (p.requests.length > 0) await take();
  };
  const first = unwrap(await h.store.acquire(ROOT, "owner", 30_000));
  const died = await resume(first, h.artifacts, h.config(), { input: ASK });

  expect(p.requests.map((r) => r.method)).toEqual(["card"]);
  expect(p.sends()).toHaveLength(0);
  // The attempt is durable, and then not one byte of it left.
  expect(types(events(first))).toContain("remote_call");
  expect(died).toMatchObject({ kind: "halted", halt: { code: "branch_busy" } });
});

test("a card read whose lease is taken while the name resolves writes nothing", async () => {
  const p = partner();
  const h = await drill(p);
  p.send = () => {
    throw new Error("this drill never reaches a send");
  };
  p.onResolve = usurps(h);
  const first = unwrap(await h.store.acquire(ROOT, "owner", 30_000));
  const died = await resume(first, h.artifacts, h.config(), { input: ASK });

  // The card read is on the send's own path, so it is fenced too: nothing is read and the
  // attempt never begins.
  expect(p.requests).toEqual([]);
  expect(types(events(first))).not.toContain("remote_call");
  expect(died).toMatchObject({ kind: "halted", halt: { code: "branch_busy" } });
});
