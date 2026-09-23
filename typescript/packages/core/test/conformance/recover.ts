import { expect } from "bun:test";
import { z } from "zod";
import type { KnownEvent } from "../../src/log";
import {
  type LoopConfig,
  type LoopEnd,
  recordedStubs,
  resume,
} from "../../src/loop";
import { scriptedModel } from "../../src/model";
import { knownEvents } from "../../src/reduce";
import type { ArtifactStore, Writer } from "../../src/store";
import { verifyExport } from "../../src/verify";
import { unwrap } from "../store/helpers";
import { type Case, type Counters, caseStore, type Matcher } from "./cases";
import { scriptedTools } from "./sandbox";

// The recover and stub kinds (spec/conformance/README.md): acquire (recovery runs first), then
// resume the loop against the scripts until the branch is idle or parked, and compare exactly
// what was appended.

const ALICE = { issuer: "api", tenant: "acme", subject: "alice" };
const HOLDER = "conformance-runner";

/** Runs a recover or stub case and compares its outcome, appended events and counters. */
export async function runAppending(c: Case, bytes: Uint8Array): Promise<void> {
  const imported = caseStore(c);
  const log = unwrap(imported.store.importLog(bytes));
  const leaf = log.segments.at(-1)?.header;
  if (leaf === undefined) throw new Error("a verified log has a header");
  const { db, store, artifacts } = imported;
  const clock = { now: c.now };
  const acquired = store.acquire(leaf.branch_id, HOLDER);
  if (!acquired.ok) {
    expect<unknown>({
      code: acquired.error.code,
      seq: acquired.error.seq,
    }).toEqual({
      code: c.error?.code,
      seq: c.error?.seq,
    });
    expect(c.appended ?? []).toEqual([]);
    return close(imported.db, db);
  }
  // Acquiring may already append (log_repaired).
  const before = log.fold.seq;

  const outcome = await run(c, acquired.value, artifacts, clock);
  const appended = knownEvents(acquired.value.chain).filter(
    (e) => e.seq > before,
  );
  if (c.error === undefined) expect(outcome.end.kind).not.toBe("halted");
  else
    expect(outcome.end.kind === "halted" ? outcome.end.halt.code : "ok").toBe(
      c.error.code,
    );
  expectAppended(appended, c.appended ?? []);
  for (const m of c.must)
    expect(appended.some((e) => matchesOne(m, e))).toBe(true);
  const exported = unwrap(store.exportBranch(leaf.branch_id));
  expect(verifyExport(exported).ok).toBe(true);
  close(imported.db, db);
}

function close(...dbs: readonly { close: () => void }[]): void {
  for (const db of new Set(dbs)) db.close();
}

type Clock = { now: number };

async function run(
  c: Case,
  writer: Writer,
  artifacts: ArtifactStore,
  clock: Clock,
): Promise<{ readonly end: LoopEnd }> {
  const model = scriptedModel(c.scripts.model ?? { responses: [] });
  const sandbox = scriptedTools(
    c.scripts.sandbox,
    writer.chain.fold.tools,
    () => clock.now,
  );
  const stubs =
    c.scripts.stubs === undefined ? undefined : recordedStubs(c.scripts.stubs);
  const output = writer.chain.fold.policy?.output;
  const config: LoopConfig = {
    models: () => model,
    tools: sandbox.tools,
    authorize: () => ({
      decision: "allow",
      source: "policy",
      rule_id: "conformance_allow",
    }),
    clock: {
      now: () => clock.now,
      sleepUntil: async (t) => {
        clock.now = Math.max(clock.now, t);
      },
    },
    principal: ALICE,
    skewMarginMs: 1000,
    ...(stubs === undefined ? {} : { stub: stubs }),
    ...(output === undefined
      ? {}
      : { output: z.fromJSONSchema(output.schema) }),
  };
  // stub input.text: the user_input the case sends once its log is imported.
  const text = z
    .strictObject({ text: z.string() })
    .optional()
    .parse(c.input)?.text;
  const end = await resume(writer, artifacts, config, {
    loop: c.scripts.model !== undefined,
    ...(text === undefined
      ? {}
      : {
          input: {
            type: "user_input",
            type_version: 1,
            critical: true,
            actor: { kind: "user", principal: ALICE },
            data: { source: "api", text },
          },
        }),
  });
  expect({
    remaining: model.remaining(),
    unexpected: model.unexpected(),
  }).toEqual({
    remaining: 0,
    unexpected: 0,
  });
  expect(pick(sandbox.counters(), c.sandbox)).toEqual(c.sandbox ?? {});
  if (stubs !== undefined)
    expect({
      consumed: stubs.consumed(),
      unmatched: stubs.unmatched(),
    }).toEqual(c.stubs ?? { consumed: 0, unmatched: 0 });
  return { end };
}

/** The counters a case lists, with 0 for a tool that never ran. */
function pick(got: Required<Counters>, want: Counters | undefined): Counters {
  const out: Record<string, Record<string, number>> = {};
  for (const [kind, tools] of Object.entries(want ?? {})) {
    const have: Readonly<Record<string, number>> =
      kind === "dispatches"
        ? got.dispatches
        : kind === "lookups"
          ? got.lookups
          : got.new_executions;
    out[kind] = Object.fromEntries(
      Object.keys(tools).map((t) => [t, have[t] ?? 0]),
    );
  }
  return out;
}

/** `appended` matching: exact count and order; data is a deep subset (conformance README). */
export function expectAppended(
  got: readonly KnownEvent[],
  want: readonly Matcher[],
): void {
  const shown = got.map((e, i) => {
    const m = want[i];
    if (m === undefined) return { type: e.type, data: e.data };
    return {
      type: e.type,
      ...(m.seq === undefined ? {} : { seq: e.seq }),
      ...(m.actor_kind === undefined ? {} : { actor_kind: e.actor.kind }),
      ...(m.epoch === undefined ? {} : { epoch: e.epoch }),
      ...(m.branch_id === undefined ? {} : { branch_id: e.branch_id }),
      ...(m.critical === undefined ? {} : { critical: e.critical }),
      ...(m.data === undefined ? {} : { data: subset(e.data, m.data) }),
    };
  });
  expect(plainJson(shown)).toEqual(plainJson(want));
}

function plainJson(value: unknown): unknown {
  return JSON.parse(JSON.stringify(value));
}

/** The keys of `actual` that `want` lists, recursively for objects (a deep-subset match). */
function subset(actual: unknown, want: unknown): unknown {
  const isObject = (v: unknown): v is object =>
    typeof v === "object" && v !== null && !Array.isArray(v);
  if (!isObject(want) || !isObject(actual)) return actual ?? null;
  return Object.fromEntries(
    Object.entries(want).map(([key, value]) => [
      key,
      subset(Reflect.get(actual, key), value),
    ]),
  );
}

/** A must matcher against one event: the same deep-subset rule as `appended`. */
function matchesOne(m: Matcher, e: KnownEvent): boolean {
  const shown = {
    type: e.type,
    ...(m.seq === undefined ? {} : { seq: e.seq }),
    ...(m.actor_kind === undefined ? {} : { actor_kind: e.actor.kind }),
    ...(m.epoch === undefined ? {} : { epoch: e.epoch }),
    ...(m.branch_id === undefined ? {} : { branch_id: e.branch_id }),
    ...(m.critical === undefined ? {} : { critical: e.critical }),
    ...(m.data === undefined ? {} : { data: subset(e.data, m.data) }),
  };
  return JSON.stringify(plainJson(shown)) === JSON.stringify(plainJson(m));
}
