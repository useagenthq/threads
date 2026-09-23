import { describe, expect, test } from "bun:test";
import { agent, openThread, scriptedModel, sqlite } from "../../src";
import { code, unwrap } from "../store/helpers";
import { childIds, priced, run, say, spawn } from "./usage-kit";

// Cost nanos are wire integers (at most 2^53 - 1): past that, cost() is cost_overflow, never a
// saturated or rounded amount (spec/api.json Thread.cost).

/** One scripted response (10 input tokens) at this price costs 6e15 nanos: within the range. */
const HUGE = { input: 600_000_000_000_000, output: 0 };

describe("cost_overflow", () => {
  test("a thread whose own cost passes 2^53 - 1 nanos", async () => {
    const store = sqlite(":memory:");
    const free = agent({
      name: "free",
      model: scriptedModel({ responses: [say("Free.")] }),
    });
    // Two responses at HUGE: 1.2e16 nanos.
    const thread = await run(
      store,
      priced([spawn("free"), say("Done.")], HUGE),
      [free],
    );
    expect(code(await thread.cost())).toBe("cost_overflow");
    expect(code(await thread.cost({ tree: true }))).toBe("cost_overflow");
  });

  test("a tree total past the range, though every thread's own cost fits", async () => {
    const store = sqlite(":memory:");
    const one = agent({ name: "one", model: priced([say("One.")], HUGE) });
    const two = agent({ name: "two", model: priced([say("Two.")], HUGE) });
    const lead = priced([spawn("one"), spawn("two"), say("Done.")]);
    const thread = await run(store, lead, [one, two]);
    expect(code(await thread.cost())).toBe("ok");
    const [first] = await childIds(thread);
    if (first === undefined) throw new Error("no child");
    const child = unwrap(await openThread(store, first));
    expect(unwrap(await child.cost())?.known_nanos).toBe(6_000_000_000_000_000);
    expect(code(await thread.cost({ tree: true }))).toBe("cost_overflow");
  });
});
