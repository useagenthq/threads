import { describe, expect, test } from "bun:test";
import { existsSync, readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { z } from "zod";
import { reduce } from "../../src/reduce";
import type { Result } from "../../src/result";
import { type VerifiedLog, verifyExport } from "../../src/verify";
import type { LogError } from "../../src/verify/error";
import { fixture } from "../store/helpers";
import { plain } from "./cases";

// The read side of every staged case whose family waits for a later build (spec/conformance/
// README.md, "Staged cases"): each log passes semantic rules 31-45 but 43 and reduces to the
// state it expects. A case that ships its artifacts is imported, so every request's Render v1
// bytes are checked too. Projections, tree walks and the index rebuild are the later builds' to
// prove.

const STAGED = join(import.meta.dir, "../../../../../spec/conformance/staged");
const Expected = z.object({
  outcome: z.enum(["ok", "error"]),
  state: z.unknown().optional(),
  states: z.record(z.string(), z.unknown()).optional(),
});
const Meta = z.object({ clock: z.object({ now: z.number() }) });

const bytes = (path: string): Uint8Array => new Uint8Array(readFileSync(path));
const json = (path: string): unknown => JSON.parse(readFileSync(path, "utf8"));

/** Every log of a case, by label: `log` for a one-log case, else each file under logs/. */
function logs(dir: string): ReadonlyMap<string, Uint8Array> {
  if (existsSync(join(dir, "log.jsonl")))
    return new Map([["log", bytes(join(dir, "log.jsonl"))]]);
  const names = readdirSync(join(dir, "logs"));
  return new Map(
    names.map((f) => [f.replace(/\.jsonl$/, ""), bytes(join(dir, "logs", f))]),
  );
}

/** The log verified, or imported with the case's artifacts when it ships them. */
async function read(
  dir: string,
  log: Uint8Array,
): Promise<Result<VerifiedLog, LogError>> {
  const root = join(dir, "artifacts");
  if (!existsSync(root)) return verifyExport(log);
  const { db, store, artifacts } = await fixture();
  for (const f of readdirSync(root)) await artifacts.put(bytes(join(root, f)));
  const imported = store.importLog(log);
  await db.close();
  return imported;
}

describe("staged cases read", () => {
  for (const name of readdirSync(STAGED).toSorted()) {
    const dir = join(STAGED, name);
    const expected = Expected.parse(json(join(dir, "expected.json")));
    const { now } = Meta.parse(json(join(dir, "case.json"))).clock;
    for (const [label, log] of logs(dir))
      test(`${name}: the ${label} log reads`, async () => {
        const verified = await read(dir, log);
        if (!verified.ok) throw new Error(JSON.stringify(verified.error));
        const want =
          expected.outcome === "ok"
            ? (expected.states?.[label] ?? expected.state)
            : undefined;
        if (want !== undefined)
          expect(plain(reduce(verified.value, now))).toEqual(want);
      });
  }
});
