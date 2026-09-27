import { describe, expect, test } from "bun:test";
import { join } from "node:path";
import {
  contextIdOf,
  messageIdOf,
  requestIdOf,
} from "../../src/outbound/derive";

// spec/conformance/vectors/a2a.json, `outbound`: the derivations a client must agree on with itself
// across processes and with the other language. The vector is generated from their definitions, not
// from either implementation's output, so a change that drifts from the contract fails here rather
// than quietly re-pinning itself. Python runs the same rows in
// python/tests/a2a/test_outbound_vectors.py.

type Row = Readonly<Record<string, string>>;

const vectors = join(
  import.meta.dir,
  "..",
  "..",
  "..",
  "..",
  "..",
  "spec",
  "conformance",
  "vectors",
  "a2a.json",
);
const tables: Readonly<Record<string, readonly Row[]>> =
  await Bun.file(vectors).json();
const rows = tables["outbound"] ?? [];

describe("the outbound derivations match the pinned vector", () => {
  test("the table is not empty", () => {
    expect(rows.length).toBeGreaterThan(0);
  });
  for (const row of rows)
    test(row["name"] ?? "", () => {
      // The messageId comes from (branch_id, call_id) alone, never from the attempt: that is what
      // makes every re-dispatch of one call look like one message to a peer that deduplicates.
      expect(messageIdOf(row["branch_id"] ?? "", row["call_id"] ?? "")).toBe(
        row["message_id"] ?? "",
      );
      expect(contextIdOf(row["thread_id"] ?? "", row["remote"] ?? "")).toBe(
        row["context_id"] ?? "",
      );
      expect(requestIdOf(row["tenant"] ?? "", row["thread_id"] ?? "")).toBe(
        row["request"] ?? "",
      );
    });
});

test("an attempt never changes the message id", () => {
  const row = rows[0];
  if (row === undefined) throw new Error("the outbound table has rows");
  const branch = row["branch_id"] ?? "";
  const call = row["call_id"] ?? "";
  expect(messageIdOf(branch, call)).toBe(messageIdOf(branch, call));
  // A different call, and only a different call, is a different message.
  expect(messageIdOf(branch, call)).not.toBe(messageIdOf(branch, `${call}x`));
});
