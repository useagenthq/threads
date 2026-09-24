import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { z } from "zod";
import { buildTar } from "../../../src/sandbox/tree/build";
import { CAPS, readTar, storeTar } from "../../../src/sandbox/tree/tar";
import {
  encodeTree,
  parseTree,
  TreeEntry,
  treeManifestHash,
} from "../../../src/sandbox/tree/tree";
import { memoryArtifacts } from "../../../src/store/artifacts";
import { chunkings } from "./chunks";

// spec/conformance/vectors: manifest-tar.json, tree.json and archive-invalid.json, each archive
// read from several chunkings.

const VECTORS = join(
  import.meta.dir,
  "../../../../../../spec/conformance/vectors",
);
const read = (name: string): unknown =>
  JSON.parse(readFileSync(join(VECTORS, name), "utf8"));

const TarVector = z.object({
  cases: z.array(
    z.object({
      name: z.string(),
      tar: z.string(),
      entries: z.array(TreeEntry),
    }),
  ),
});
const TreeVector = z.object({
  cases: z.array(
    z.object({
      name: z.string(),
      tree: z.string(),
      sha256: z.string(),
      manifest_hash: z.string(),
    }),
  ),
  invalid: z.array(z.object({ name: z.string(), tree: z.string() })),
});
const InvalidVector = z.object({
  cases: z.array(
    z.object({
      name: z.string(),
      tar: z.string(),
      caps: z.object({ file: z.number(), total: z.number() }).optional(),
      reason: z.string(),
      entry: z.string().nullable(),
    }),
  ),
});

const tars = TarVector.parse(read("manifest-tar.json")).cases;
const trees = TreeVector.parse(read("tree.json"));
const invalid = InvalidVector.parse(read("archive-invalid.json")).cases;
const utf8 = new TextEncoder();

describe("manifest-tar.json", () => {
  for (const [i, c] of tars.entries()) {
    test(c.name, async () => {
      const bytes = Uint8Array.fromBase64(c.tar);
      const want = trees.cases.find((t) => t.name === c.name);
      for (const chunks of chunkings(bytes, i)) {
        const artifacts = memoryArtifacts();
        const stored = await storeTar(chunks(), artifacts);
        if (!stored.ok) throw new Error(stored.error.message);
        expect(stored.value.tree.entries).toEqual(c.entries);
        expect(new TextDecoder().decode(encodeTree(stored.value.tree))).toBe(
          want?.tree ?? "",
        );
        expect(stored.value.sha256).toBe(want?.sha256 ?? "");
        expect(stored.value.manifest_hash).toBe(want?.manifest_hash ?? "");
      }
    });
  }
});

describe("tree.json", () => {
  for (const c of trees.cases) {
    test(`${c.name} parses back and rebuilds to the same tree`, async () => {
      const tree = parseTree(utf8.encode(c.tree));
      if (!tree.ok) throw new Error(tree.error.message);
      expect(treeManifestHash(tree.value)).toBe(c.manifest_hash);
      // The file artifacts come from reading the vector's archive.
      const artifacts = memoryArtifacts();
      const source = tars.find((t) => t.name === c.name)?.tar ?? "";
      await readTar(
        (async function* () {
          yield Uint8Array.fromBase64(source);
        })(),
        artifacts.sink,
      );
      const parts: Uint8Array[] = [];
      const built = buildTar(
        tree.value,
        artifacts,
        { uid: 1000, gid: 1000 },
        (b) => parts.push(b.slice()),
      );
      expect(built.ok).toBe(true);
      const again = await readTar(
        (async function* () {
          yield* parts;
        })(),
        memoryArtifacts().sink,
      );
      expect(again).toEqual({ ok: true, value: tree.value });
    });
  }
  for (const c of trees.invalid) {
    test(`refuses a tree: ${c.name}`, () => {
      const tree = parseTree(utf8.encode(c.tree));
      expect(tree.ok ? "ok" : tree.error.code).toBe("artifact_corrupt");
    });
  }
});

describe("archive-invalid.json", () => {
  for (const [i, c] of invalid.entries()) {
    test(c.name, async () => {
      const bytes = Uint8Array.fromBase64(c.tar);
      for (const chunks of chunkings(bytes, i)) {
        const result = await readTar(
          chunks(),
          memoryArtifacts().sink,
          c.caps ?? CAPS,
        );
        if (result.ok) throw new Error(`${c.name} was accepted`);
        const { code, reason, entry } = result.error;
        expect({ code, reason: String(reason), entry }).toEqual({
          code: "archive_invalid",
          reason: c.reason,
          entry: c.entry,
        });
      }
    });
  }
});
