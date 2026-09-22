import { readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import type { z } from "zod";
import { canonicalize } from "../src/log";
import { specSchema } from "./spec-schema";

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

const [flag, file = SPEC] = process.argv.slice(2);
const schema = specSchema();
if (flag === "--check") {
  process.exitCode = check(schema, file);
} else {
  writeFileSync(SPEC, format(schema));
  console.log(`wrote ${SPEC}`);
}
