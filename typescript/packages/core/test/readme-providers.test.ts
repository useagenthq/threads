import { describe, expect, test } from "bun:test";
import { mkdtempSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

// Every provider factory call in README.md and the provider docs pages compiles against today's
// options, so a renamed or unknown option in the docs fails here (lane 15B's runnable proof,
// until lane 11's example runner executes these blocks). A known-bad call proves the check bites.

const ROOT = resolve(import.meta.dir, "../../../..");
const TS = join(ROOT, "typescript");
const GUIDES = join(ROOT, "docs/content/docs/(guides)");
const FACTORIES = ["e2b", "daytona", "slack", "github", "whatsapp"] as const;
const BLOCK = /```ts\n([\s\S]*?)```/g;
const CALL = new RegExp(`\\b(${FACTORIES.join("|")})\\(`, "g");

const pages = (dir: string): string[] =>
  readdirSync(dir)
    .filter((f) => f.endsWith(".mdx"))
    .map((f) => join(dir, f));
const PAGES = [
  join(ROOT, "README.md"),
  ...pages(join(GUIDES, "sandboxes")),
  ...pages(join(GUIDES, "host")),
];

/** The call expression starting at `start`, up to its balancing parenthesis. */
function callAt(code: string, start: number): string {
  let depth = 0;
  for (let i = code.indexOf("(", start); i < code.length; i++) {
    depth += code[i] === "(" ? 1 : code[i] === ")" ? -1 : 0;
    if (depth === 0) return code.slice(start, i + 1);
  }
  throw new Error(`unbalanced call at ${code.slice(start, start + 40)}`);
}

function calls(text: string): { factory: string; expr: string }[] {
  return [...text.matchAll(BLOCK)].flatMap(([, block = ""]) =>
    [...block.matchAll(CALL)].map((m) => ({
      factory: m[1] ?? "",
      expr: callAt(block, m.index),
    })),
  );
}

function source(exprs: readonly string[]): string {
  const imports = FACTORIES.map(
    (f) => `import { ${f} } from "${join(TS, "packages", f, "src/index")}";`,
  );
  return [
    `import { secret } from "${join(TS, "packages/core/src/index")}";`,
    ...imports,
    ...exprs.map((e, i) => `export const call${i} = (): unknown => ${e};`),
    "",
  ].join("\n");
}

/** tsc's error lines, by file name. */
function compile(files: Readonly<Record<string, string>>): string[] {
  const dir = mkdtempSync(join(tmpdir(), "readme-providers-"));
  for (const [name, text] of Object.entries(files))
    writeFileSync(join(dir, name), text);
  writeFileSync(
    join(dir, "tsconfig.json"),
    JSON.stringify({
      extends: join(TS, "tsconfig.base.json"),
      compilerOptions: {
        noEmit: true,
        skipLibCheck: true,
        // Without DOM, WebSocket is Bun's, whose constructor takes headers (daytona).
        lib: ["ESNext"],
        typeRoots: [join(TS, "node_modules/@types")],
        types: ["bun"],
      },
      include: ["*.ts"],
    }),
  );
  const tsc = Bun.spawnSync([join(TS, "node_modules/.bin/tsc"), "-p", "."], {
    cwd: dir,
  });
  return tsc.stdout
    .toString()
    .split("\n")
    .filter((l) => l.includes("error TS"));
}

describe("provider calls in README.md and the docs", () => {
  const found = PAGES.flatMap((page) => calls(readFileSync(page, "utf8")));

  test("every TypeScript provider factory is documented", () => {
    expect(new Set(found.map((c) => c.factory))).toEqual(new Set(FACTORIES));
  });

  test("each compiles, and an unknown option doesn't", () => {
    const errors = compile({
      "docs.ts": source(found.map((c) => c.expr)),
      "bad.ts": source(["e2b({ internet: true })"]),
    });
    expect(errors.filter((l) => !l.startsWith("bad.ts"))).toEqual([]);
    expect(errors.some((l) => l.startsWith("bad.ts"))).toBe(true);
  }, 60_000);
});
