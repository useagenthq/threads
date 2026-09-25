import { expect, test } from "bun:test";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { z } from "zod";
import { int8, pauseMs, RETRY_BUDGET_MS } from "../src/driver";
import { lockKey } from "../src/install";
import { numbered } from "../src/placeholders";

// The driver's parts that need no server.

const Vectors = z.object({
  cases: z.array(
    z.object({ name: z.string(), sql: z.string(), pg: z.string() }),
  ),
});

test("the ? rewrite passes the shared placeholder vectors", () => {
  const path = join(
    import.meta.dir,
    "../../../../spec/conformance/vectors/sql-placeholders.json",
  );
  const { cases } = Vectors.parse(JSON.parse(readFileSync(path, "utf8")));
  expect(cases.length).toBeGreaterThan(0);
  for (const c of cases)
    expect([c.name, numbered(c.sql)]).toEqual([c.name, c.pg]);
});

test("int8 is a number, and past 2^53 - 1 a bug, not an outage", () => {
  expect(int8("9007199254740991")).toBe(Number.MAX_SAFE_INTEGER);
  expect(int8("-42")).toBe(-42);
  expect(() => int8("9007199254740993")).toThrow("safe integer range");
});

test("the advisory lock key is sha256('threads-store' || schema), a signed int64", () => {
  const digest = createHash("sha256").update("threads-storepublic").digest();
  expect(lockKey("public")).toBe(digest.readBigInt64BE(0));
  expect(lockKey("a")).not.toBe(lockKey("b"));
});

test("a conflict's pause doubles from 10 ms to at most 250 ms, jittered, within a 5 s budget", () => {
  const most = (n: number): number => pauseMs(n, () => 1);
  expect([1, 2, 3, 4, 5, 6, 7].map(most)).toEqual([
    10, 20, 40, 80, 160, 250, 250,
  ]);
  expect(pauseMs(9, () => 0)).toBe(0);
  expect(RETRY_BUDGET_MS).toBe(5_000);
  // Even at the longest pauses, the budget holds more than twenty attempts.
  let spent = 0;
  let attempts = 0;
  while (spent < RETRY_BUDGET_MS) spent += most(++attempts);
  expect(attempts).toBeGreaterThan(20);
});
