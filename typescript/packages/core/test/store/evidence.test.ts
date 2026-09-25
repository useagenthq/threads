import { describe, expect, test } from "bun:test";
import { sha256Hex } from "../../src/hash";
import { canonicalLine } from "../../src/store/encode";
import { caseStore, loadCase } from "../conformance/cases";
import {
  code,
  fixture,
  ROOT,
  rows,
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
async function withoutHead(): Promise<Uint8Array> {
  const f = await fixture();
  unwrap(await f.store.createBranch(THREAD, ROOT));
  const writer = unwrap(await f.store.acquire(ROOT, "holder-a"));
  unwrap(await writer.append([started, userInput("hi")]));
  const bytes = unwrap(await f.store.exportBranch(ROOT));
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
  test("a torn tail is stored as an artifact and exported back, unverified", async () => {
    const torn = new TextEncoder().encode("x".repeat(57));
    const input = join2(await withoutHead(), torn);
    const { store, db } = await fixture();
    unwrap(await store.importLog(input));
    expect(unwrap(await store.exportBranch(ROOT))).toEqual(input);
    const log = unwrap(await store.read(ROOT));
    expect(log.headVerified).toBe(false);
    expect(log.torn?.bytes).toEqual(torn);
    expect(await rows(db, "SELECT state, head_verified FROM branches")).toEqual(
      [{ state: "inspection_only", head_verified: 0 }],
    );
  });

  test("acquiring a torn import records log_repaired and makes it runnable", async () => {
    const torn = new TextEncoder().encode("x".repeat(57));
    const prefix = await withoutHead();
    const { store, db } = await fixture();
    unwrap(await store.importLog(join2(prefix, torn)));
    const writer = unwrap(await store.acquire(ROOT, "holder-a"));
    const repaired = writer.chain.events.at(-1)?.event;
    expect(repaired?.type).toBe("log_repaired");
    expect(repaired?.critical).toBe(false);
    expect(repaired?.data).toEqual({
      truncated_bytes: 57,
      at_offset: prefix.length,
      dropped_ref: {
        sha256: sha256Hex(torn),
        bytes: 57,
        media_type: "application/octet-stream",
      },
    });
    expect(await rows(db, "SELECT state, head_verified FROM branches")).toEqual(
      [{ state: "ready", head_verified: 1 }],
    );
    expect(unwrap(await store.read(ROOT)).headVerified).toBe(true);
  });

  test("a terminated prefix without its head line stays unverified", async () => {
    const input = await withoutHead();
    const { store } = await fixture();
    unwrap(await store.importLog(input));
    expect(unwrap(await store.exportBranch(ROOT))).toEqual(input);
    expect(unwrap(await store.read(ROOT)).headVerified).toBe(false);
    expect(code(await store.acquire(ROOT, "holder-a"))).toBe(
      "branch_not_runnable",
    );
  });
});

describe("re-importing onto a stored leaf compares its head evidence", () => {
  const tail = (text: string): Uint8Array => new TextEncoder().encode(text);
  for (const [what, first, second, expected] of [
    ["the same torn tail", "AAA", "AAA", "ok"],
    ["the same verified head", "head", "head", "ok"],
    ["the same missing head", "", "", "ok"],
    ["another torn tail", "AAA", "BBB", "seq_conflict"],
    ["verified, then unverified", "head", "", "seq_conflict"],
    ["no tail, then a torn tail", "", "AAA", "seq_conflict"],
  ] as const) {
    test(`${what}: ${expected}`, async () => {
      const prefix = headerOnly("threads-ts", "0.0.0");
      const body = prefix.subarray(0, prefix.indexOf(0x0a) + 1);
      const build = (t: string): Uint8Array =>
        t === "head" ? prefix : join2(body, tail(t));
      const { store } = await fixture();
      unwrap(await store.importLog(build(first)));
      expect(code(await store.importLog(build(second)))).toBe(expected);
    });
  }
});

describe("only the branch's own writer appends", () => {
  for (const [what, impl, version] of [
    ["another implementation", "threads-py", "0.0.0"],
    ["another major version", "threads-ts", "1.0.0"],
  ] as const) {
    test(`${what}: read and export work, acquire is writer_mismatch`, async () => {
      const input = headerOnly(impl, version);
      const { store } = await fixture();
      unwrap(await store.importLog(input));
      expect(unwrap(await store.exportBranch(ROOT))).toEqual(input);
      expect(unwrap(await store.read(ROOT)).headVerified).toBe(true);
      const refused = await store.acquire(ROOT, "holder-a");
      expect(
        refused.ok ? "ok" : [refused.error.code, refused.error.seq],
      ).toEqual(["writer_mismatch", 0]);
    });
  }
});

describe("an in-doubt branch requires recovery", () => {
  test("an effect begun before a crash flags the writer", async () => {
    const c = loadCase("effect-crash-after-begin-idempotent");
    const { store } = await caseStore(c);
    const log = unwrap(await store.importLog(c.log ?? new Uint8Array()));
    const branch = log.segments.at(-1)?.header.branch_id;
    if (branch === undefined) throw new Error("a verified log has a header");
    expect(
      unwrap(await store.acquire(branch, "holder-a")).requiresRecovery,
    ).toBe(true);
  });

  test("a model request awaiting its response flags the writer", async () => {
    const c = loadCase("model-response-recovered-by-lookup");
    const { store } = await caseStore(c);
    const log = unwrap(await store.importLog(c.log ?? new Uint8Array()));
    const branch = log.segments.at(-1)?.header.branch_id;
    if (branch === undefined) throw new Error("a verified log has a header");
    expect(
      unwrap(await store.acquire(branch, "holder-a")).requiresRecovery,
    ).toBe(true);
  });

  test("a settled branch does not", async () => {
    const f = await fixture();
    unwrap(await f.store.createBranch(THREAD, ROOT));
    const writer = unwrap(await f.store.acquire(ROOT, "holder-a"));
    expect(writer.requiresRecovery).toBe(false);
  });
});
