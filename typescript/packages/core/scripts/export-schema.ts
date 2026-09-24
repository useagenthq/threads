import { readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { z } from "zod";
import { canonicalize } from "../src/log";
import { ModelCatalog } from "../src/model/catalog";
import { evalSchema } from "./eval-schema";
import { Tree } from "../src/sandbox/tree/tree";
import { specSchema } from "./spec-schema";
import { toolCatalog, toolSchema } from "./tool-catalog";

// bun scripts/export-schema.ts           write spec/schema/events.v1.schema.json
// bun scripts/export-schema.ts --check   fail if the committed file differs; list every difference
// bun scripts/export-schema.ts --check F  the same against another file F

type Json = z.core.util.JSONType;

const SPEC = join(
  import.meta.dir,
  "../../../../spec/schema/events.v1.schema.json",
);

/** RFC 8785 key order, two-space indent: stable bytes that still read well in a diff. */
function format(value: Json): string {
  const canonical = canonicalize(value);
  if (!canonical.ok) throw new Error(canonical.error.message);
  return `${JSON.stringify(JSON.parse(canonical.value), null, 2)}\n`;
}

/** Every JSON pointer where `a` and `b` differ. */
function differences(a: Json, b: Json, path = ""): string[] {
  if (isObject(a) && isObject(b)) {
    const keys = new Set([...Object.keys(a), ...Object.keys(b)]);
    return [...keys].toSorted().flatMap((key) => {
      const at = `${path}/${key}`;
      if (!(key in a)) return [`${at}: only in the committed file`];
      if (!(key in b)) return [`${at}: only in the export`];
      return differences(a[key] ?? null, b[key] ?? null, at);
    });
  }
  if (Array.isArray(a) && Array.isArray(b) && a.length === b.length) {
    return a.flatMap((item, i) =>
      differences(item, b[i] ?? null, `${path}/${i}`),
    );
  }
  return JSON.stringify(a) === JSON.stringify(b)
    ? []
    : [
        `${path || "/"}: export ${JSON.stringify(a)} ≠ committed ${JSON.stringify(b)}`,
      ];
}

function isObject(value: Json): value is { [key: string]: Json } {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function check(schema: Json, file: string): number {
  const committed = readFileSync(file, "utf8");
  if (committed === format(schema)) return 0;
  const found = differences(schema, JSON.parse(committed));
  for (const line of found) console.error(line);
  console.error(
    found.length === 0
      ? `${file}: same schema, not in canonical form; run bun run schema:export`
      : `${file}: ${found.length} differences from the Zod export; run bun run schema:export`,
  );
  return 1;
}

const TOOLS = join(
  import.meta.dir,
  "../../../../spec/schema/tools.v1.schema.json",
);
const CATALOG = join(
  import.meta.dir,
  "../../../../spec/schema/tools.v1.catalog.json",
);

/** The catalog is compared byte for byte: it is the exact text both runtimes pin. */
function checkBytes(expected: string, file: string): number {
  if (readFileSync(file, "utf8") === expected) return 0;
  console.error(
    `${file}: differs from the Zod catalog; run bun run schema:export`,
  );
  return 1;
}

const MODEL_CATALOG = join(
  import.meta.dir,
  "../../../../spec/schema/model-catalog.v1.schema.json",
);

/** The shape of spec/models/<provider>.v1.json. */
function catalogSchema(): Json {
  const exported = z.toJSONSchema(ModelCatalog, {
    target: "draft-2020-12",
    io: "input",
    unrepresentable: "throw",
  });
  return {
    ...JSON.parse(JSON.stringify(exported)),
    $id: "urn:threads:schema:model-catalog:v1",
  };
}

const EVAL = join(
  import.meta.dir,
  "../../../../spec/schema/eval.v1.schema.json",
);

const TREE = join(
  import.meta.dir,
  "../../../../spec/schema/tree.v1.schema.json",
);

/** The tree artifact (spec/schema/README.md, Snapshot manifest). */
function treeSchema(): Json {
  const exported = z.toJSONSchema(Tree, {
    target: "draft-2020-12",
    io: "input",
    unrepresentable: "throw",
  });
  return {
    ...JSON.parse(JSON.stringify(exported)),
    $id: "urn:threads:schema:tree:v1",
  };
}

const [flag, file = SPEC] = process.argv.slice(2);
const schema = specSchema();
if (flag === "--check") {
  process.exitCode =
    check(schema, file) +
    (file === SPEC
      ? check(toolSchema(), TOOLS) +
        checkBytes(toolCatalog(), CATALOG) +
        check(catalogSchema(), MODEL_CATALOG) +
        check(evalSchema(schema), EVAL) +
        check(treeSchema(), TREE)
      : 0);
} else {
  writeFileSync(SPEC, format(schema));
  writeFileSync(TOOLS, format(toolSchema()));
  writeFileSync(CATALOG, toolCatalog());
  writeFileSync(MODEL_CATALOG, format(catalogSchema()));
  writeFileSync(EVAL, format(evalSchema(schema)));
  writeFileSync(TREE, format(treeSchema()));
  console.log(
    `wrote ${SPEC}, ${TOOLS}, ${CATALOG}, ${MODEL_CATALOG}, ${EVAL} and ${TREE}`,
  );
}
