import { describe, expect, test } from "bun:test";
import { projections, reduce } from "../../src/reduce";
import { type VerifiedLog, verifyExport } from "../../src/verify";
import { fixture, unwrap } from "../store/helpers";
import { CASE_NAMES, type Case, type Kind, loadCase, plain } from "./cases";

// One runner for every case in spec/conformance/cases, no per-case code. `reduce` cases run in
// full. Other kinds need features after step 2a; their log still imports and reduces here, and
// what they need beyond that is skipped by name with the reason below, never silently.

const LATER: Readonly<Record<Kind, string | undefined>> = {
  reduce: undefined,
  render: "Render v1 re-render, C7 and next-request bytes need the renderer",
  recover:
    "appending (log_repaired, recovery) needs the loop and the scripted model and sandbox",
  fork: "fork() needs the sandbox adapter and the resource ledger",
  stub: "stub mode needs the stub gateway",
  intake: "intake needs the host webhook pipeline",
  policy: "policy needs the permission engine",
  security: "no runner yet for this reserved kind",
  parity: "no runner yet for this reserved kind",
};

// Fixtures that break a semantic rule the spec states. The test pins the rejection, so it
// fails (and must be removed) once the generator is fixed.
const SPEC_DEFECTS: Readonly<
  Record<string, { code: string; seq: number; why: string }>
> = {
  "render-reference-framing-escaped": {
    code: "invalid_transition",
    seq: 19,
    why: "rule 10: summary_ref is not the text of its summary_request_event_id response",
  },
};

function checkState(c: Case, log: VerifiedLog): void {
  if (c.state !== undefined) expect(plain(reduce(log, c.now))).toEqual(c.state);
  if (c.projections !== undefined) {
    const all: Record<string, unknown> = { ...projections(log) };
    for (const [key, value] of Object.entries(c.projections))
      expect(plain(all[key])).toEqual(value);
  }
  if (c.committedBytes !== undefined)
    expect(log.committedBytes).toBe(c.committedBytes);
  expect(log.headVerified).toBe(c.headVerified);
}

function runReduce(c: Case, bytes: Uint8Array): void {
  const log = verifyExport(bytes);
  if (c.error === undefined) {
    if (!log.ok) throw new Error(`${log.error.code}: ${log.error.message}`);
    checkState(c, log.value);
    return;
  }
  expect(log.ok ? "ok" : log.error.code).toBe(c.error.code);
  if (!log.ok && c.error.seq !== undefined)
    expect(log.error.seq).toBe(c.error.seq);
}

/** The import-level part of a later kind: the input log imports and reduces as expected. */
function runImport(c: Case, bytes: Uint8Array): void {
  const log = verifyExport(bytes);
  if (!log.ok) throw new Error(`${log.error.code}: ${log.error.message}`);
  checkState(c, log.value);
}

/**
 * Replay: SQLite storage and JSONL are one contract. The log stored and exported again is the
 * same bytes (a torn tail is dropped, so only its prefix is), and it reduces to the same state.
 */
function replay(c: Case, bytes: Uint8Array): void {
  const direct = unwrap(verifyExport(bytes));
  const { db, store } = fixture();
  unwrap(store.importLog(bytes));
  const leaf = direct.segments.at(-1)?.header.branch_id;
  if (leaf === undefined) throw new Error("a verified log has a header");
  const exported = unwrap(store.exportBranch(leaf));
  if (direct.torn === undefined) expect(exported).toEqual(bytes);
  else
    expect(exported.subarray(0, direct.committedBytes)).toEqual(
      bytes.subarray(0, direct.committedBytes),
    );
  const stored = unwrap(store.read(leaf));
  expect(reduce(stored, c.now)).toEqual(reduce(direct, c.now));
  expect(projections(stored)).toEqual(projections(direct));
  db.close();
}

describe("replay: every importable case log round-trips through SQLite", () => {
  for (const name of CASE_NAMES) {
    const c = loadCase(name);
    const { log } = c;
    if (log === undefined || !verifyExport(log).ok) continue;
    test(name, () => replay(c, log));
  }
});

describe("conformance", () => {
  for (const name of CASE_NAMES) {
    const c = loadCase(name);
    const later = LATER[c.kind];
    const { log } = c;
    const defect = SPEC_DEFECTS[name];
    if (defect !== undefined && log !== undefined) {
      test(`${c.kind}: ${name} (spec defect, rejected: ${defect.why})`, () => {
        const result = verifyExport(log);
        const got = result.ok ? "ok" : [result.error.code, result.error.seq];
        expect(plain(got)).toEqual([defect.code, defect.seq]);
      });
    } else if (later === undefined && log !== undefined) {
      test(`${c.kind}: ${name}`, () => runReduce(c, log));
    } else if (log !== undefined) {
      test(`${c.kind}: ${name} (import and state only; skipped: ${later})`, () =>
        runImport(c, log));
    } else {
      test.skip(`${c.kind}: ${name} (skipped: ${later})`, () => {});
    }
  }
});
