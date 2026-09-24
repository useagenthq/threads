import { expect } from "bun:test";
import { z } from "zod";
import { matches } from "../../src/evals/compare";
import { rerun } from "../../src/evals/rerun";
import type { KnownEvent } from "../../src/log";
import { SandboxScript } from "../../src/sandbox";
import type { Case, Counters, Matcher } from "./cases";

// The recover and stub kinds (spec/conformance/README.md): acquire (recovery runs first), then
// resume the loop against the scripts until the branch is idle or parked, and compare exactly
// what was appended. The run itself is the eval runner's rerun (src/evals/rerun.ts): one
// implementation for the corpus and for saved cases.

const ALICE = { issuer: "api", tenant: "acme", subject: "alice" };

/** Runs a recover or stub case and compares its outcome, appended events and counters. */
export async function runAppending(c: Case, bytes: Uint8Array): Promise<void> {
  // stub input.text: the user_input the case sends once its log is imported.
  const text = z
    .strictObject({ text: z.string() })
    .optional()
    .parse(c.input)?.text;
  const outcome = await rerun({
    log: bytes,
    artifacts: c.artifacts,
    now: c.now,
    model: c.scripts.model,
    sandbox:
      c.scripts.sandbox === undefined
        ? undefined
        : SandboxScript.parse(c.scripts.sandbox),
    stubs: c.scripts.stubs,
    extensions: undefined,
    input:
      text === undefined
        ? undefined
        : {
            type: "user_input",
            type_version: 1,
            critical: true,
            actor: { kind: "user", principal: ALICE },
            data: { source: "api", text },
          },
    recorded: undefined,
  });
  if (outcome.kind === "refused") {
    expect<unknown>({ code: outcome.code, seq: outcome.seq }).toEqual({
      code: c.error?.code,
      seq: c.error?.seq,
    });
    expect(c.appended ?? []).toEqual([]);
    return;
  }
  const { end, appended } = outcome;
  if (c.error === undefined) expect(end.kind).not.toBe("halted");
  else expect(end.kind === "halted" ? end.halt.code : "ok").toBe(c.error.code);
  expectAppended(appended, c.appended ?? []);
  for (const m of c.must)
    expect(appended.some((e) => matches(m, e))).toBe(true);
  expect({
    remaining: outcome.scriptLeft,
    unexpected: outcome.unexpected,
  }).toEqual({
    remaining: 0,
    unexpected: 0,
  });
  expect(pick(outcome.counters, c.sandbox)).toEqual(c.sandbox ?? {});
  if (outcome.stubs !== undefined)
    expect(outcome.stubs).toEqual(c.stubs ?? { consumed: 0, unmatched: 0 });
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
