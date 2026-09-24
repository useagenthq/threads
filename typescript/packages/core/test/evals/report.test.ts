import { expect, test } from "bun:test";
import { caseLine, dollars } from "../../src/evals/report";

// The CLI's per-case line and the cost figure in the summary (spec lane 22, D).

test("a stale case names its drift once, then what was left unchecked", () => {
  const drift = {
    ok: false,
    kinds: ["model" as const],
    unchecked: ["mcp:jira", "extension:crm"],
  };
  expect(
    caseLine({
      name: "c",
      status: "stale",
      reason: "drift: model",
      checks: { drift },
    }),
  ).toBe("STALE c drift: model; unchecked mcp:jira, extension:crm");
  expect(
    caseLine({
      name: "c",
      status: "passed",
      checks: { drift: { ok: true, kinds: [], unchecked: ["memory"] } },
    }),
  ).toBe("PASS c (drift: unchecked memory)");
});

test("dollars round half up to the cent", () => {
  expect([
    dollars(0),
    dollars(4_999_999),
    dollars(5_000_000),
    dollars(380_000_000),
  ]).toEqual(["$0.00", "$0.00", "$0.01", "$0.38"]);
});
