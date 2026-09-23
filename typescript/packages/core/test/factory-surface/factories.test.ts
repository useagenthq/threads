import { describe, expect, test } from "bun:test";
import { mkdirSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

// The TypeScript factory signature checks (spec/tools/gen_api_surface_factories.py), compiled
// by tsc over a fixture contract: the matching package is green, and each kind of drift fails
// at the assertion that names it.

const ROOT = resolve(import.meta.dir, "../../../../..");
const TS = join(ROOT, "typescript");
const GEN = join(ROOT, "spec/tools/gen_api_surface_factories.py");
const FIXTURE = join(import.meta.dir, "fixtures/api.json");

const GOOD = `import type { SearchBackend } from "CORE";
export type Transport = { readonly send: (url: string) => void };
export declare function make(
  id: string,
  options: {
    readonly region: string;
    readonly tenant?: (phoneNumberId: string) => string;
    readonly fetch?: typeof globalThis.fetch;
    readonly transport?: Transport;
  },
): SearchBackend;
`;

// Each case edits the matching package once; the value is the assertion that must fail.
const CASES: Record<string, readonly [string, string, string]> = {
  good: ["", "", ""],
  arity: ["id: string,", "id: string, extra: string,", "make_arity"],
  extraKey: [
    "readonly region: string;",
    "readonly region: string; readonly zone?: string;",
    "make_option_keys",
  ],
  missingKey: [
    "readonly fetch?: typeof globalThis.fetch;",
    "",
    "make_option_keys",
  ],
  optionType: [
    "readonly region: string;",
    "readonly region: number;",
    "make_region",
  ],
  requiredFlag: [
    "readonly region: string;",
    "readonly region?: string;",
    "make_region_required",
  ],
  callbackParam: [
    "(phoneNumberId: string) => string",
    "(phoneNumberId: number) => string",
    "make_tenant",
  ],
  platformType: ["typeof globalThis.fetch;", "() => void;", "make_fetch"],
  // A bare platform name is the package's own export: a different one is red.
  ownPlatformType: [
    "readonly transport?: Transport;",
    "readonly transport?: { readonly send: (url: number) => void };",
    "make_transport",
  ],
  returnType: [
    "): SearchBackend;",
    "): { readonly nope: true };",
    "make_returns",
  ],
};

function compile(): { found: Map<string, Set<string>>; stray: string[] } {
  const dir = mkdtempSync(join(tmpdir(), "factory-surface-"));
  const core = join(TS, "packages/core/src/index");
  const api = JSON.parse(readFileSync(FIXTURE, "utf8"));
  api.packages.core.ts = core;
  writeFileSync(join(dir, "api.json"), JSON.stringify(api));
  writeFileSync(
    join(dir, "tsconfig.json"),
    JSON.stringify({
      extends: join(TS, "tsconfig.base.json"),
      compilerOptions: {
        noEmit: true,
        skipLibCheck: true,
        typeRoots: [join(TS, "node_modules/@types")],
        types: ["bun"],
      },
      include: ["**/*.ts"],
    }),
  );
  for (const [name, [from, to]] of Object.entries(CASES)) {
    const at = join(dir, name);
    mkdirSync(at);
    writeFileSync(
      join(at, "pkg.ts"),
      GOOD.replace("CORE", core).replace(from, to),
    );
    const gen = Bun.spawnSync(
      [
        "python3",
        GEN,
        "--api",
        join(dir, "api.json"),
        "--ts-package",
        "fix",
      ].concat(["--ts-out", join(at, "factories.ts")]),
    );
    expect(gen.exitCode).toBe(0);
  }
  const tsc = Bun.spawnSync([join(TS, "node_modules/.bin/tsc"), "-p", "."], {
    cwd: dir,
  });
  const out = tsc.stdout.toString();
  // An error outside the generated checks means the harness itself is broken.
  const stray = out
    .split("\n")
    .filter((l) => l.includes("error TS") && !/^\w+\/factories\.ts\(/.test(l));
  return { found: failures(dir, out), stray };
}

// case -> the names of the generated assertions tsc reported, by line.
function failures(dir: string, out: string): Map<string, Set<string>> {
  const found = new Map<string, Set<string>>();
  for (const m of out.matchAll(/^(\w+)\/factories\.ts\((\d+),/gm)) {
    const [, name = "", line = "0"] = m;
    const source = readFileSync(join(dir, name, "factories.ts"), "utf8");
    const at = source.split("\n")[Number(line) - 1] ?? "";
    const assertion = /^export type (\w+) =/.exec(at)?.[1] ?? at;
    found.set(name, (found.get(name) ?? new Set()).add(assertion));
  }
  return found;
}

describe("factory signature checks", () => {
  const { found, stray } = compile();

  test("the matching package compiles, and nothing else fails", () => {
    expect(stray).toEqual([]);
    expect(found.get("good")).toBeUndefined();
  });

  for (const [name, [, , assertion]] of Object.entries(CASES)) {
    if (name === "good") continue;
    test(`${name}: fails at ${assertion}`, () => {
      expect([...(found.get(name) ?? [])]).toContain(assertion);
    });
  }
});
