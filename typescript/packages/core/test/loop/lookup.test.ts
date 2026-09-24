import { describe, expect, test } from "bun:test";
import { AssertionError } from "node:assert";
import { assertNever } from "../../src/assert-never";
import { lookedUp } from "../../src/loop/lookup";
import { StoreError } from "../../src/store/driver";

// A lookup that throws answers unknown, unless it is the store's outage or a bug: a broken
// invariant (the repo's own assertNever included) surfaces as a bug, as in Python.

const unknown = (reason: string) => ({ status: "unknown" as const, reason });

/** An exhaustive switch; a value outside its union reaches assertNever at run time. */
function label(kind: "known"): string {
  switch (kind) {
    case "known":
      return kind;
    default:
      return assertNever(kind);
  }
}

describe("lookedUp", () => {
  test("a provider's error answers unknown", async () => {
    const answer = await lookedUp(async () => {
      throw Object.assign(new Error("refused"), { code: "ECONNREFUSED" });
    }, unknown);
    expect(answer).toEqual({
      status: "unknown",
      reason: "the lookup failed: Error",
    });
  });

  test("a store outage and a broken invariant still throw", async () => {
    expect(() => label(JSON.parse('"surprise"'))).toThrow(AssertionError);
    for (const fault of [
      new StoreError("disk I/O error"),
      new AssertionError({ message: "broke" }),
    ])
      await expect(
        lookedUp(async () => {
          throw fault;
        }, unknown),
      ).rejects.toBe(fault);
    await expect(
      lookedUp(
        async () => ({ status: label(JSON.parse('"surprise"')) }),
        (reason) => ({
          status: reason,
        }),
      ),
    ).rejects.toBeInstanceOf(AssertionError);
  });
});
