import { describe, expect, test } from "bun:test";
import { sha256Hex } from "../../src/hash";
import type { KnownEvent } from "../../src/log";
import { knownEvents, projections, reduce } from "../../src/reduce";
import { refReader, render } from "../../src/render";
import type { ArtifactStore } from "../../src/store";
import { type VerifiedLog, verifyExport } from "../../src/verify";
import { unwrap } from "../store/helpers";
import {
  CASE_NAMES,
  type Case,
  caseStore,
  type Kind,
  loadCase,
  plain,
} from "./cases";
import { runFork } from "./fork";
import { runAppending } from "./recover";

// One runner for every case in spec/conformance/cases, no per-case code. `reduce` and `render`
// cases run in full. Other kinds need features after step 2b; their log still imports and reduces here, and
// what they need beyond that is skipped by name with the reason below, never silently.

const LATER: Readonly<Record<Kind, string | undefined>> = {
  reduce: undefined,
  render: undefined,
  recover: undefined,
  fork: undefined,
  stub: undefined,
  intake: "run by packages/host/test/conformance.test.ts",
  host: "run by packages/host/test/conformance.test.ts",
  policy: undefined,
  security: "no runner yet for this reserved kind",
  parity: "no runner yet for this reserved kind",
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

/**
 * Render kind: import into a store holding the case's artifacts (C7 and the re-render of every
 * recorded request run there), then the next request and the history-prefix property.
 */
function runRender(c: Case, bytes: Uint8Array): void {
  const { db, store, artifacts } = caseStore(c);
  const log = store.importLog(bytes);
  db.close();
  const events = log.ok ? knownEvents(log.value) : [];
  const rendered = log.ok ? render(events, refReader(artifacts)) : log;
  if (c.error !== undefined) {
    expect(rendered.ok ? "ok" : rendered.error.code).toBe(c.error.code);
    if (!rendered.ok) expect(rendered.error.seq).toBe(c.error.seq);
    return;
  }
  checkState(c, unwrap(log));
  const next = unwrap(rendered);
  expect(next.bytes).toEqual(c.request ?? new Uint8Array());
  expect(sha256Hex(next.bytes)).toBe(c.render?.next_request_sha256 ?? "");
  expect({
    bytes: next.prefix.length,
    sha256: sha256Hex(next.prefix),
  }).toEqual(c.render?.declared_prefix ?? { bytes: 0, sha256: "" });
  historyPrefix(events, artifacts, next.bytes);
}

/** Events after which the next turn request may legitimately stop extending the last one. */
function breaksHistory(e: KnownEvent): boolean {
  if (e.type === "hook_decision")
    return (
      e.data.hook === "before_input" &&
      (e.data.decision === "deny" || e.data.decision === "failed")
    );
  return ["compacted", "context_edited", "settings_changed"].includes(e.type);
}

/**
 * Cache reuse, checked apart from C7: each turn request's bytes (the recorded ones, then the
 * next) start with the previous turn request's, unless an edit, compaction, settings change or
 * denied input lies between them. Compaction side requests are skipped.
 */
function historyPrefix(
  events: readonly KnownEvent[],
  artifacts: ArtifactStore,
  next: Uint8Array,
): void {
  let last: Uint8Array | undefined;
  for (const e of events) {
    if (breaksHistory(e)) last = undefined;
    if (e.type !== "model_request" || e.data.purpose === "compaction") continue;
    const bytes = unwrap(artifacts.get(e.data.request_ref.sha256));
    if (last !== undefined)
      expect(bytes.subarray(0, last.length)).toEqual(last);
    last = bytes;
  }
  if (last !== undefined) expect(next.subarray(0, last.length)).toEqual(last);
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
  const { db, store } = caseStore(c);
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
    // A render case that expects an error is refused by import itself.
    if (log === undefined || !verifyExport(log).ok) continue;
    if (c.kind === "render" && c.error !== undefined) continue;
    test(name, () => replay(c, log));
  }
});

/** The runner for one case: its kind's, or import and state only for a later kind. */
function runnerFor(
  c: Case,
  log: Uint8Array,
): (() => void | Promise<void>) | undefined {
  switch (c.kind) {
    case "recover":
    case "stub":
      return async () => {
        runImport(c, log);
        await runAppending(c, log);
      };
    case "fork":
      return async () => {
        if (c.state !== undefined) runImport(c, log);
        await runFork(c, log);
      };
    case "render":
      return () => runRender(c, log);
    case "reduce":
      return () => runReduce(c, log);
    default:
      return undefined;
  }
}

describe("conformance", () => {
  for (const name of CASE_NAMES) {
    const c = loadCase(name);
    const later = LATER[c.kind];
    const { log } = c;
    // test/permissions/policy.test.ts runs the policy cases.
    if (c.kind === "policy") continue;
    const run = log === undefined ? undefined : runnerFor(c, log);
    if (run !== undefined) test(`${c.kind}: ${name}`, run);
    else if (log !== undefined)
      test(`${c.kind}: ${name} (import and state only; skipped: ${later})`, () =>
        runImport(c, log));
    else test.skip(`${c.kind}: ${name} (skipped: ${later})`, () => {});
  }
});
