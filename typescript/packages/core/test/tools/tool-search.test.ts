import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { z } from "zod";
import { loadedTools, parseRender } from "../../src/model/render-lines";
import { UNICODE_VERSION } from "../../src/tools/generated/fold-table";
import { fold, search, terms, tokens } from "../../src/tools/tool-search";

// The shared tool_search vectors (spec/conformance/vectors), and the lint grep that keeps the
// runtime's own Unicode helpers out of the matcher.

const VECTORS = join(
  import.meta.dir,
  "../../../../../spec/conformance/vectors",
);
const read = (name: string): unknown =>
  JSON.parse(readFileSync(join(VECTORS, name), "utf8"));

const Fold = z.object({
  unicode_version: z.string(),
  cases: z.array(
    z.object({
      input: z.string(),
      fold: z.string(),
      tokens: z.array(z.string()),
    }),
  ),
});
const Split = z.object({
  cases: z.array(z.object({ query: z.string(), terms: z.array(z.string()) })),
});
const Search = z.object({
  cases: z.array(
    z.object({
      query: z.string(),
      limit: z.number(),
      deferred: z.array(
        z.object({ name: z.string(), description: z.string() }),
      ),
      loaded: z.array(z.string()),
      lines: z.array(z.string()),
      loads: z.array(z.string()),
    }),
  ),
});
const Provider = z.object({
  cases: z.array(
    z.object({
      name: z.string(),
      request: z.array(z.unknown()),
      tools: z.array(z.unknown()),
    }),
  ),
});

describe("tool_search vectors", () => {
  test("fold and tokens", () => {
    const v = Fold.parse(read("unicode-fold.json"));
    expect(v.unicode_version).toBe(UNICODE_VERSION);
    for (const c of v.cases)
      expect({ fold: fold(c.input), tokens: tokens(c.input) }).toEqual({
        fold: c.fold,
        tokens: c.tokens,
      });
  });

  test("query split", () => {
    for (const c of Split.parse(read("query-split.json")).cases)
      expect({ q: c.query, terms: terms(c.query) }).toEqual({
        q: c.query,
        terms: c.terms,
      });
  });

  test("search", () => {
    for (const c of Search.parse(read("tool-search.json")).cases) {
      const found = search(c.query, c.limit, c.deferred, c.loaded);
      expect({ q: c.query, lines: found.lines, loads: found.loaded }).toEqual({
        q: c.query,
        lines: c.lines,
        loads: c.loads,
      });
    }
  });

  test("provider tools: the latest complete set without stubs, then the loaded specs", () => {
    const encoder = new TextEncoder();
    for (const c of Provider.parse(read("provider-tools.json")).cases) {
      const body = encoder.encode(
        c.request.map((l) => `${JSON.stringify(l)}\n`).join(""),
      );
      expect<unknown>({
        name: c.name,
        tools: loadedTools(parseRender(body)),
      }).toEqual({
        name: c.name,
        tools: c.tools,
      });
    }
  });
});

test("the matcher never uses the runtime's case mapping, trimming or whitespace classes", () => {
  const source = readFileSync(
    join(import.meta.dir, "../../src/tools/tool-search.ts"),
    "utf8",
  );
  for (const banned of [
    "toLowerCase",
    "toUpperCase",
    "toLocaleLowerCase",
    "toLocaleUpperCase",
    "trim",
    "split(/\\s",
    "\\s",
  ])
    expect({ banned, found: source.includes(banned) }).toEqual({
      banned,
      found: false,
    });
});
