import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { sha256Hex } from "../../src/hash";
import { canonicalLine } from "../../src/store/encode";
import {
  code,
  fixture,
  ROOT,
  started,
  THREAD,
  unwrap,
  userInput,
} from "./helpers";

const NL = new Uint8Array([0x0a]);

function join2(...parts: readonly Uint8Array[]): Uint8Array {
  const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
  let at = 0;
  for (const part of parts) {
    out.set(part, at);
    at += part.length;
  }
  return out;
}

/** A clean export's lines without its head line (every line still ends in "\n"). */
function withoutHead(): Uint8Array {
  const f = fixture();
  unwrap(f.store.createBranch(THREAD, ROOT));
  const writer = unwrap(f.store.acquire(ROOT, "holder-a"));
  unwrap(writer.append([started, userInput("hi")]));
  const bytes = unwrap(f.store.exportBranch(ROOT));
  return bytes.subarray(0, bytes.lastIndexOf(0x0a, bytes.length - 2) + 1);
}

/** A one-line branch whose header names `impl` at `version`, closed by its head line. */
function headerOnly(impl: string, version: string): Uint8Array {
  const header = unwrap(
    canonicalLine({
      format: "threads.log",
      format_version: 1,
      thread_id: THREAD,
      branch_id: ROOT,
      created_at: 1,
      writer: { impl, version },
    }),
  );
  const head = unwrap(
    canonicalLine({
      format: "threads.head",
      format_version: 1,
      branch_id: ROOT,
      seq: 0,
      hash: sha256Hex(header),
    }),
  );
  return join2(header, NL, head, NL);
}

describe("an import without a verified head keeps its evidence", () => {
  test("a torn tail is stored as an artifact and exported back, unverified", () => {
    const torn = new TextEncoder().encode("x".repeat(57));
    const input = join2(withoutHead(), torn);
    const { store, db } = fixture();
    unwrap(store.importLog(input));
    expect(unwrap(store.exportBranch(ROOT))).toEqual(input);
    const log = unwrap(store.read(ROOT));
    expect(log.headVerified).toBe(false);
    expect(log.torn?.bytes).toEqual(torn);
    expect(db.all("SELECT state, head_verified FROM branches", [])).toEqual([
      { state: "inspection_only", head_verified: 0 },
    ]);
    expect(code(store.acquire(ROOT, "holder-a"))).toBe("branch_not_runnable");
  });

  test("a terminated prefix without its head line stays unverified", () => {
    const input = withoutHead();
    const { store } = fixture();
    unwrap(store.importLog(input));
    expect(unwrap(store.exportBranch(ROOT))).toEqual(input);
    expect(unwrap(store.read(ROOT)).headVerified).toBe(false);
    expect(code(store.acquire(ROOT, "holder-a"))).toBe("branch_not_runnable");
  });
});

describe("only the branch's own writer appends", () => {
  for (const [what, impl, version] of [
    ["another implementation", "threads-py", "0.0.0"],
    ["another major version", "threads-ts", "1.0.0"],
  ] as const) {
    test(`${what}: read and export work, acquire is writer_mismatch`, () => {
      const input = headerOnly(impl, version);
      const { store } = fixture();
      unwrap(store.importLog(input));
      expect(unwrap(store.exportBranch(ROOT))).toEqual(input);
      expect(unwrap(store.read(ROOT)).headVerified).toBe(true);
      const refused = store.acquire(ROOT, "holder-a");
      expect(
        refused.ok ? "ok" : [refused.error.code, refused.error.seq],
      ).toEqual(["writer_mismatch", 0]);
    });
  }
});

describe("an in-doubt branch requires recovery", () => {
  test("an effect begun before a crash flags the writer", () => {
    const path = join(
      import.meta.dir,
      "../../../../../spec/conformance/cases/effect-crash-after-begin-idempotent/log.threads-ts.jsonl",
    );
    const { store } = fixture();
    const log = unwrap(store.importLog(new Uint8Array(readFileSync(path))));
    const branch = log.segments.at(-1)?.header.branch_id;
    if (branch === undefined) throw new Error("a verified log has a header");
    expect(unwrap(store.acquire(branch, "holder-a")).requiresRecovery).toBe(
      true,
    );
  });

  test("a settled branch does not", () => {
    const f = fixture();
    unwrap(f.store.createBranch(THREAD, ROOT));
    const writer = unwrap(f.store.acquire(ROOT, "holder-a"));
    expect(writer.requiresRecovery).toBe(false);
  });
});
