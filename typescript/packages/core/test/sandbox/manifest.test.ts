import { expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { z } from "zod";
import { manifestHash, manifestOf } from "../../src/sandbox";

// Snapshot manifests order paths by UTF-16 code units (spec/conformance/vectors).
const VECTOR = join(
  import.meta.dir,
  "../../../../../spec/conformance/vectors/manifest-order.json",
);
const Vector = z.object({
  files: z.array(z.object({ path: z.string(), data: z.string() })),
  manifest_paths: z.array(z.string()),
  manifest_hash: z.string(),
});

test("the manifest matches the shared vector", () => {
  const vector = Vector.parse(JSON.parse(readFileSync(VECTOR, "utf8")));
  const encoder = new TextEncoder();
  const tree = new Map(
    vector.files.map((f) => [f.path, encoder.encode(f.data)] as const),
  );
  const manifest = manifestOf(tree);
  expect(manifest.map((e) => e.path)).toEqual(vector.manifest_paths);
  expect(manifestHash(manifest)).toBe(vector.manifest_hash);
});
