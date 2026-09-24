import { describe, expect, test } from "bun:test";
import { existsSync, readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { z } from "zod";
import type { DryPin } from "../../src/agent/registry";
import { sqlite, tenantStore } from "../../src/agent/sqlite";
import { caseNames } from "../../src/evals/case-dir";
import { evalsOf } from "../../src/evals/run";
import { canonicalize, ThreadStartedData } from "../../src/log";

// spec/conformance/evals (lane 22): every directory's saved cases give its report.json, byte for
// byte, offline, with the dry pins in its agents.json standing in for the user's agents.

const ROOT = join(import.meta.dir, "../../../../../spec/conformance/evals");

const Pins = z.array(
  z.strictObject({
    started: ThreadStartedData,
    mcp: z.array(z.string()),
    setup_extensions: z.array(z.string()),
    setup_providers: z.array(z.enum(["memory", "knowledge"])),
  }),
);

function pinsOf(dir: string): readonly DryPin[] | undefined {
  const path = join(dir, "agents.json");
  if (!existsSync(path)) return undefined;
  return Pins.parse(JSON.parse(readFileSync(path, "utf8"))).map((p) => ({
    started: p.started,
    mcp: p.mcp,
    setupExtensions: p.setup_extensions,
    setupProviders: p.setup_providers,
    leadsTeam: false,
  }));
}

function canonical(value: unknown): string {
  const text = canonicalize(JSON.parse(JSON.stringify(value)));
  if (!text.ok) throw new Error("a report is canonical JSON");
  return text.value;
}

describe("spec/conformance/evals", () => {
  for (const name of readdirSync(ROOT).toSorted())
    test(name, async () => {
      const dir = join(ROOT, name);
      const root = join(dir, "cases");
      const report = await evalsOf(
        {
          root,
          pins: pinsOf(dir),
          agents: [],
          live: undefined,
          store: tenantStore(sqlite(":memory:"), "evals"),
          kept: false,
        },
        caseNames(root),
        {},
      );
      const want = readFileSync(join(dir, "report.json"), "utf8");
      expect(JSON.parse(`${canonical(report)}`)).toEqual(JSON.parse(want));
      expect(`${canonical(report)}\n`).toBe(want);
    });
});
