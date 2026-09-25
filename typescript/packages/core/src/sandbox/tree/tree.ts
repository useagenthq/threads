import { z } from "zod";
import { sha256Hex } from "../../hash";
import { canonicalize, type Json } from "../../log";
import { parseStrictJson } from "../../log/json";
import type { Lit, Strict } from "../../log/zod-types";
import { err, ok, type Result } from "../../result";
import { type LogError, logError } from "../../verify/error";
import { manifestHash } from "../fake";
import { decodeUtf8, inRoot, isNormal, pathSet } from "./paths";

// The tree artifact (spec/schema/tree.v1.schema.json): one sandbox file tree as RFC 8785 JSON,
// each file's content its own artifact. Exported from Zod; Python's model is generated from it.

const Path: z.ZodString = z
  .string()
  .min(1)
  .describe(
    "Relative to the root, '/'-separated, with no empty, '.' or '..' component.",
  );
const Mode: z.ZodInt = z
  .int()
  .min(0)
  .max(0o7777)
  .describe("Permission bits, as `stat -c %a` reads them.");

export const TreeEntry: z.ZodDiscriminatedUnion<
  [
    Strict<{
      path: typeof Path;
      kind: Lit<"file">;
      mode: typeof Mode;
      size: z.ZodInt;
      sha256: z.ZodString;
    }>,
    Strict<{ path: typeof Path; kind: Lit<"dir">; mode: typeof Mode }>,
    Strict<{ path: typeof Path; kind: Lit<"symlink">; target: z.ZodString }>,
  ],
  "kind"
> = z
  .discriminatedUnion("kind", [
    z
      .strictObject({
        path: Path,
        kind: z.literal("file"),
        mode: Mode,
        size: z.int().min(0).max(Number.MAX_SAFE_INTEGER),
        sha256: z
          .string()
          .regex(/^[0-9a-f]{64}$/)
          .describe("The artifact holding the file's bytes."),
      })
      .meta({ id: "TreeFile" }),
    z
      .strictObject({ path: Path, kind: z.literal("dir"), mode: Mode })
      .meta({ id: "TreeDir" }),
    z
      .strictObject({
        path: Path,
        kind: z.literal("symlink"),
        target: z
          .string()
          .min(1)
          .describe("Relative, and lexically inside the root."),
      })
      .meta({ id: "TreeSymlink" }),
  ])
  .meta({ id: "TreeEntry" });
export type TreeEntry = z.infer<typeof TreeEntry>;

export const Tree: Strict<{
  tree_version: Lit<1>;
  entries: z.ZodArray<typeof TreeEntry>;
}> = z
  .strictObject({
    tree_version: z.literal(1),
    entries: z.array(TreeEntry),
  })
  .describe(
    "A sandbox file tree; its semantic rules are in spec/schema/README.md, Snapshot manifest.",
  );
export type Tree = z.infer<typeof Tree>;

/** The tree's artifact bytes: RFC 8785 canonical JSON. */
export function encodeTree(tree: Tree): Uint8Array {
  const value: Json = {
    tree_version: tree.tree_version,
    entries: tree.entries.map((e) => ({ ...e })),
  };
  const text = canonicalize(value);
  if (!text.ok) throw new Error("a tree is JSON");
  return new TextEncoder().encode(text.value);
}

/** The snapshot manifest hash of the tree's files (spec/schema/README.md, Snapshot manifest). */
export function treeManifestHash(tree: Tree): string {
  return manifestHash(
    tree.entries.flatMap((e) =>
      e.kind === "file"
        ? [{ path: e.path, mode: e.mode, size: e.size, sha256: e.sha256 }]
        : [],
    ),
  );
}

/**
 * The first semantic rule (2 to 5) the entries break, in order; undefined when they keep every
 * rule. The schema can't hold these, so every tree is checked before it is trusted or built.
 */
export function broken(entries: readonly TreeEntry[]): string | undefined {
  const paths = pathSet();
  let last: string | undefined;
  for (const e of entries) {
    if (last !== undefined && !(last < e.path))
      return `${e.path} is out of order`;
    last = e.path;
    if (!isNormal(e.path)) return `${e.path} is not a normal path`;
    if (e.kind === "symlink" && !inRoot(e.path, e.target))
      return `${e.path} links outside the root`;
    const clash = paths.add(e.path, e.kind);
    if (clash !== undefined) return `${e.path}: ${clash}`;
  }
  return undefined;
}

/**
 * Parses a tree artifact's bytes (storage is a boundary): strict JSON in canonical form, the
 * schema, then the semantic rules. Anything else is artifact_corrupt.
 */
export function parseTree(bytes: Uint8Array): Result<Tree, LogError> {
  const corrupt = (why: string): Result<Tree, LogError> =>
    err(
      logError(
        "artifact_corrupt",
        `tree ${sha256Hex(bytes)} is invalid: ${why}`,
      ),
    );
  const text = decodeUtf8(bytes);
  if (text === undefined) return corrupt("not UTF-8");
  const json = parseStrictJson(text);
  if (!json.ok) return corrupt(json.error.message);
  const tree = Tree.safeParse(json.value);
  if (!tree.success) return corrupt(tree.error.message);
  const canonical = encodeTree(tree.data);
  if (
    canonical.length !== bytes.length ||
    canonical.some((b, i) => b !== bytes[i])
  )
    return corrupt("not in canonical form");
  const why = broken(tree.data.entries);
  return why === undefined ? ok(tree.data) : corrupt(why);
}
