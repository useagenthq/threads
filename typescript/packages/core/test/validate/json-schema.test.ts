import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { z } from "zod";
import { jsonSchema } from "../../src/agent/tool";
import { conforms, holds, unchecked } from "../../src/validate/json-schema";

// The rule 20 reader (spec/schema/README.md, Output schemas): the same answers as the Python
// reader, on the shared multipleOf vector, each format, and what Zod exports.

const VECTOR = join(
  import.meta.dir,
  "../../../../../spec/conformance/vectors/multiple-of.json",
);
const Vector = z.object({
  cases: z.array(
    z.object({ value: z.number(), divisor: z.number(), multiple: z.boolean() }),
  ),
});

test("multipleOf is decimal-exact on the shared vector", () => {
  const { cases } = Vector.parse(JSON.parse(readFileSync(VECTOR, "utf8")));
  expect(cases.map((c) => holds({ multipleOf: c.divisor }, c.value))).toEqual(
    cases.map((c) => c.multiple),
  );
});

describe("formats", () => {
  test.each([
    ["date-time", "2024-02-29T23:59:59.5+05:30", "2023-02-29T10:00:00Z"],
    ["date", "2024-02-29", "2024-13-01"],
    ["time", "10:00:00Z", "10:00:00"],
    ["email", "a.b+c@x-y.example.com", "a@b"],
    ["uri", "urn:isbn:0451450523", "example.com/x"],
    [
      "uuid",
      "123E4567-e89b-12d3-a456-426614174000",
      "123e4567e89b12d3a456426614174000",
    ],
  ])("%s accepts and rejects", (format, good, bad) => {
    expect(holds({ format }, good)).toBe(true);
    expect(holds({ format }, bad)).toBe(false);
    expect(holds({ format }, 7)).toBe(true);
  });
});

type Node = {
  readonly name: string;
  readonly children?: readonly Node[] | undefined;
};
const Node: z.ZodType<Node> = z.object({
  name: z.string().regex(/^[a-z]+$/),
  get children() {
    return z.array(Node).optional();
  },
});

test("what Zod exports is checked in full, a self-reference (#) included", () => {
  const schema = jsonSchema(
    "output",
    z.object({
      at: z.iso.datetime({ offset: true }),
      id: z.uuid(),
      mail: z.email(),
      link: z.url(),
      step: z.number().multipleOf(0.25),
      tree: Node,
    }),
  );
  expect(unchecked(schema)).toBeUndefined();
  const good = {
    at: "2026-09-23T10:00:00+02:00",
    id: "123e4567-e89b-12d3-a456-426614174000",
    mail: "ops@example.com",
    link: "https://example.com",
    step: 1.75,
    tree: { name: "root", children: [{ name: "leaf" }] },
  };
  expect(holds(schema, good)).toBe(true);
  expect(holds(schema, { ...good, step: 1.8 })).toBe(false);
  const deep = { name: "root", children: [{ name: "Leaf" }] };
  expect(holds(schema, { ...good, tree: deep })).toBe(false);
  expect(holds(jsonSchema("output", Node), deep)).toBe(false);
});

test.each([
  [{ type: "string", format: "hostname" }, "unsupported format 'hostname'"],
  [{ properties: { pair: { prefixItems: [] } } }, "'prefixItems'"],
  [{ items: { $ref: "https://example.com/x" } }, "unsupported $ref"],
])("unchecked names what the reader can't check", (schema, named) => {
  expect(unchecked(schema)).toContain(named);
});

const vector = (name: string): unknown =>
  JSON.parse(readFileSync(join(VECTOR, "..", name), "utf8"));

test("pattern is the portable subset on the shared vector", () => {
  const { cases } = z
    .object({
      cases: z.array(
        z.object({
          pattern: z.string(),
          text: z.string(),
          result: z.enum(["match", "no_match", "unsupported"]),
        }),
      ),
    })
    .parse(vector("pattern.json"));
  for (const c of cases) {
    const schema = { pattern: c.pattern };
    if (c.result === "unsupported") {
      expect(unchecked(schema), c.pattern).toBeDefined();
      continue;
    }
    expect(unchecked(schema), c.pattern).toBeUndefined();
    expect(holds(schema, c.text), c.pattern).toBe(c.result === "match");
  }
});

test("const and enum use JSON equality on the shared vector", () => {
  const { cases } = z
    .object({
      cases: z.array(
        z.object({ a: z.json(), b: z.json(), equal: z.boolean() }),
      ),
    })
    .parse(vector("json-equal.json"));
  for (const c of cases) {
    expect(holds({ const: c.a }, c.b)).toBe(c.equal);
    expect(holds({ enum: ["other", c.a] }, c.b)).toBe(c.equal);
  }
});

test.each([
  [{ $ref: "#" }, "loop"],
  [
    {
      $defs: { A: { anyOf: [{ $ref: "#" }] } },
      allOf: [{ $ref: "#/$defs/A" }],
    },
    "loop",
  ],
  [{ not: { $ref: "#/$defs/B" }, $defs: { B: { $ref: "#/$defs/B" } } }, "loop"],
  [{ items: { $ref: "#/$defs/Missing" } }, "missing definition"],
])(
  "a $ref that loops or names nothing is refused and never throws",
  (schema, named) => {
    expect(unchecked(schema)).toContain(named);
    expect(conforms(schema, {})).toBe(false);
  },
);

test("a $ref that reaches into the value is recursion, not a loop", () => {
  const schema = { properties: { next: { $ref: "#" } }, type: "object" };
  expect(unchecked(schema)).toBeUndefined();
  expect(holds(schema, { next: { next: {} } })).toBe(true);
  expect(holds(schema, { next: { next: 1 } })).toBe(false);
});
