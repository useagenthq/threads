import { afterAll, describe, expect, test } from "bun:test";
import {
  copyFileSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

// The generator (spec/tools/gen_api_surface.py) over fixture contracts and packages, compiled by
// tsc: each case checks tsc's verdict and the generated line it reports.

const here = import.meta.dir;
const root = join(here, "..", "..", "..", "..", "..");
const tsc = join(root, "typescript", "node_modules", ".bin", "tsc");
const fixtures = join(here, "fixtures");
const pkg = readFileSync(join(fixtures, "pkg.d.ts"), "utf8");
const api = readFileSync(join(fixtures, "api.json"), "utf8");
const gaps = readFileSync(join(fixtures, "gaps.json"), "utf8");
const host = readFileSync(join(fixtures, "host.d.ts"), "utf8");
const work = mkdtempSync(join(tmpdir(), "threads-api-surface-"));
let cases = 0;

afterAll(() => rmSync(work, { recursive: true, force: true }));

// four's overloads in the fixture package, and pieces for replacing them.
const FOUR = [
  "{ readonly a: string }",
  '{ readonly a: "x" }',
  '{ readonly a: "y" }',
  "{ readonly a: string; readonly d: number }",
];
const OMITS_A = "{ readonly d?: number }";
const NEEDS_A = "{ readonly a: string; readonly d?: number }";
const SEEN_FOUR =
  "export type function_four_overloads = Assert<EveryOverloadSeen<typeof core.four>>;";
const A_REQUIRED =
  'export type option_four_a_required = Assert<Equals<OptionRequired<typeof core.four, 0, "a">, true>>;';

/**
 * The fixture package with four's overloads replaced, one per options type. A `returns` of ""
 * means each entry already ends with its own return type.
 */
function withFour(options: readonly string[], returns = "): void"): string {
  return replaceFour(
    options
      .map((o) => `export declare function four(options: ${o}${returns};\n`)
      .join(""),
  );
}

/** The fixture package with four's overload declarations replaced by the given text. */
function replaceFour(overloads: string): string {
  const start = pkg.indexOf("export declare function four(");
  const end = pkg.indexOf("export type Model");
  expect(start).toBeGreaterThan(-1);
  expect(end).toBeGreaterThan(start);
  return `${pkg.slice(0, start)}${overloads}\n${pkg.slice(end)}`;
}

type Verdict = { readonly ok: boolean; readonly failed: readonly string[] };

/** Generates and compiles one case; failed holds the generated lines tsc reports. */
function compile(inputs: {
  readonly pkg?: string;
  readonly api?: string;
  readonly gaps?: string;
}): Verdict {
  cases += 1;
  const dir = join(work, `case-${cases}`);
  mkdirSync(dir);
  writeFileSync(join(dir, "pkg.d.ts"), inputs.pkg ?? pkg);
  writeFileSync(join(dir, "host.d.ts"), host);
  writeFileSync(join(dir, "api.json"), inputs.api ?? api);
  writeFileSync(join(dir, "gaps.json"), inputs.gaps ?? gaps);
  copyFileSync(join(here, "helpers.ts"), join(dir, "helpers.ts"));
  writeFileSync(join(dir, "tsconfig.json"), tsconfig(dir));
  const out = join(dir, "generated", "surface.ts");
  const generate = Bun.spawnSync([
    "python3",
    join(root, "spec", "tools", "gen_api_surface.py"),
    ...["--api", join(dir, "api.json"), "--gaps", join(dir, "gaps.json")],
    ...["--out", out],
  ]);
  expect(generate.stderr.toString()).toBe("");
  const run = Bun.spawnSync([tsc, "-p", dir, "--pretty", "false"]);
  const lines = readFileSync(out, "utf8").split("\n");
  const failed = [
    ...run.stdout.toString().matchAll(/surface\.ts\((\d+),\d+\)/g),
  ].map((m) => lines[Number(m[1]) - 1] ?? "");
  return { ok: run.exitCode === 0, failed };
}

function tsconfig(dir: string): string {
  return JSON.stringify({
    extends: join(root, "typescript", "tsconfig.base.json"),
    compilerOptions: {
      noEmit: true,
      paths: {
        "@fixture/core": [join(dir, "pkg.d.ts")],
        "@fixture/host": [join(dir, "host.d.ts")],
      },
    },
    include: ["generated/surface.ts"],
  });
}

describe("options over overloads", () => {
  test("1 to 4 overloads with different options: present and required are computed", () => {
    expect(compile({})).toEqual({ ok: true, failed: [] });
  });

  test("a wrong required flag in the contract is red at that option", () => {
    // The first required option in the fixture contract is one's a.
    const wrong = api.replace('"required": true', '"required": false');
    const verdict = compile({ api: wrong });
    expect(verdict.ok).toBe(false);
    expect(verdict.failed).toEqual([
      'export type option_one_a_required = Assert<Equals<OptionRequired<typeof core.one, 0, "a">, false>>;',
    ]);
  });

  test("an optional option flipped to required in the package is red", () => {
    const flipped = pkg.replace("readonly b?: number;", "readonly b: number;");
    const verdict = compile({ pkg: flipped });
    expect(verdict.failed).toEqual([
      'export type option_one_b_required = Assert<Equals<OptionRequired<typeof core.one, 0, "b">, false>>;',
    ]);
  });

  test("a fifth overload is still read in full", () => {
    const five = withFour([...FOUR, '{ readonly a: "z" }']);
    expect(compile({ pkg: five })).toEqual({ ok: true, failed: [] });
  });

  test("a sixth distinct overload is red: the window can't show every overload", () => {
    const six = withFour([
      ...FOUR,
      '{ readonly a: "z" }',
      '{ readonly a: "w" }',
    ]);
    expect(compile({ pkg: six }).failed).toEqual([SEEN_FOUR]);
  });

  test("an early overload that omits a required option is seen behind five identical ones", () => {
    const hidden = withFour([OMITS_A, ...Array(5).fill(NEEDS_A)]);
    const verdict = compile({ pkg: hidden });
    expect(verdict.failed).toContain(SEEN_FOUR);
    expect(verdict.failed).toContain(A_REQUIRED);
  });

  test("an early overload behind eight that differ only in return type is red", () => {
    const later = [1, 2, 3, 4, 5, 6, 7, 8].map((n) => `${NEEDS_A}): ${n}`);
    const verdict = compile({
      pkg: withFour([`${OMITS_A}): void`, ...later], ""),
    });
    expect(verdict.failed).toContain(SEEN_FOUR);
  });

  test.each([
    [8, false],
    [9, true],
  ])(
    "an oldest overload behind %i that differ only in `this` is red (by its option, or by the window)",
    (n, hidden) => {
      const later = Array.from(
        { length: n - 1 },
        (_, i) => `this: { readonly t${i}: 1 }, options: ${NEEDS_A}): void`,
      );
      const overloads = [`options: ${OMITS_A}): void`, ...later]
        .map((o) => `export declare function four(${o};\n`)
        .join("");
      const verdict = compile({ pkg: replaceFour(overloads) });
      expect(verdict.failed).toContain(hidden ? SEEN_FOUR : A_REQUIRED);
    },
  );

  test.each([
    ["six, the oldest two identical", 2, 4],
    ["seven, the oldest three identical", 3, 4],
  ])(
    "%s: every overload is seen, so a missing required option is red",
    (_, same, distinct) => {
      const others = Array.from(
        { length: distinct },
        (_, i) => `{ readonly a: "${i}" }`,
      );
      const verdict = compile({
        pkg: withFour([...Array(same).fill(OMITS_A), ...others]),
      });
      expect(verdict.failed).toEqual([A_REQUIRED]);
    },
  );
});

describe("optional methods and gaps", () => {
  test("an optional method as an optional property of the base type is green", () => {
    expect(compile({}).ok).toBe(true);
  });

  test("the optional method absent is red (missing)", () => {
    const absent = pkg.replace("  readonly lookup?: () => void;\n", "");
    expect(compile({ pkg: absent }).failed).toEqual([
      'export type method_Model_lookup_present = Assert<HasKey<core.Model, "lookup">>;',
      'export type method_Model_lookup_callable = Assert<IsCallable<core.Model["lookup"]>>;',
      'export type method_Model_lookup_overloads = Assert<EveryOverloadSeen<core.Model["lookup"]>>;',
    ]);
  });

  test("the optional method absent and listed as a missing gap is green", () => {
    const absent = pkg.replace("  readonly lookup?: () => void;\n", "");
    const listed = gaps.replace(
      "[\n",
      '[\n  {"name": "Model.lookup", "lang": "ts", "kind": "missing", "lane": "01-api-surface-gate"},\n',
    );
    expect(compile({ pkg: absent, gaps: listed })).toEqual({
      ok: true,
      failed: [],
    });
  });

  test("the optional method declared required is red (required_mismatch)", () => {
    const required = pkg.replace("readonly lookup?:", "readonly lookup:");
    expect(compile({ pkg: required }).failed).toEqual([
      'export type method_Model_lookup_required = Assert<Equals<Req<core.Model, "lookup">, false>>;',
    ]);
  });

  test("a listed gap whose member now exists is red at its IsMissing assertion", () => {
    const fixed = pkg.replace(
      "readonly send: () => void;",
      "readonly send: () => void;\n  readonly close: () => void;",
    );
    expect(compile({ pkg: fixed }).failed).toEqual([
      'export type method_Model_close_missing = Assert<IsMissing<core.Model, "close">>;',
    ]);
  });

  test("a listed missing type that now exists is red", () => {
    const fixed = `${pkg}export type Absent = { readonly id: string };\n`;
    const verdict = compile({ pkg: fixed });
    expect(verdict.ok).toBe(false);
    expect(verdict.failed).toContain(
      'export type type_Absent_missing = Assert<Equals<keyof core.Absent, "__surfaceGap">>;',
    );
  });

  test("a placement gap is red when the type isn't at the entry it names either", () => {
    const gone = pkg.replace(
      "export type Moved = { readonly id: string };\n",
      "",
    );
    expect(compile({ pkg: gone }).failed).toEqual([
      "export type type_Moved_at_core = core.Moved;",
    ]);
  });
});

describe("fields and callability", () => {
  test("a required field of an object type deleted is red", () => {
    const deleted = pkg.replace("readonly name: string; ", "");
    expect(compile({ pkg: deleted }).failed).toEqual([
      'export type field_Skill_name_present = Assert<HasKey<core.Skill, "name">>;',
      'export type field_Skill_name_required = Assert<Equals<Req<core.Skill, "name">, true>>;',
    ]);
  });

  test("a required field made optional is red", () => {
    const optional = pkg.replace(
      "readonly name: string; ",
      "readonly name?: string; ",
    );
    expect(compile({ pkg: optional }).failed).toEqual([
      'export type field_Skill_name_required = Assert<Equals<Req<core.Skill, "name">, true>>;',
    ]);
  });

  test("a method that is not callable is red", () => {
    const value = pkg.replace(
      "readonly send: () => void;",
      "readonly send: number;",
    );
    expect(compile({ pkg: value }).failed).toContain(
      'export type method_Model_send_callable = Assert<IsCallable<core.Model["send"]>>;',
    );
  });

  test("a method typed never is red", () => {
    const never = pkg.replace(
      "readonly send: () => void;",
      "readonly send: never;",
    );
    expect(compile({ pkg: never }).failed).toContain(
      'export type method_Model_send_callable = Assert<IsCallable<core.Model["send"]>>;',
    );
  });

  test("an optional field deleted is red", () => {
    const deleted = pkg.replace(" readonly shortNote?: string", "");
    expect(compile({ pkg: deleted }).failed).toEqual([
      'export type field_Skill_short_note_present = Assert<HasKey<core.Skill, "shortNote">>;',
    ]);
  });

  test("an optional field made required is red", () => {
    const required = pkg.replace("readonly shortNote?:", "readonly shortNote:");
    expect(compile({ pkg: required }).failed).toEqual([
      'export type field_Skill_short_note_required = Assert<Equals<Req<core.Skill, "shortNote">, false>>;',
    ]);
  });

  test("a listed missing type exported as an empty interface is red", () => {
    const empty = `${pkg}export interface Absent {}\n`;
    expect(compile({ pkg: empty }).failed).toContain(
      "  type Absent = { readonly __surfaceGap: true };",
    );
  });
});
