import { describe, expect, test } from "bun:test";
import {
  chmodSync,
  linkSync,
  mkdirSync,
  mkdtempSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { sha256Hex } from "../../../src/hash";
import { storeTar } from "../../../src/sandbox/tree/tar";
import type { TreeEntry } from "../../../src/sandbox/tree/tree";
import { memoryArtifacts } from "../../../src/store/artifacts";

// A real tree archived by the host's `tar -cf - -C dir .` (GNU tar on Linux, bsdtar on macOS)
// reads as the tree on disk. The hash of a tree exported this way equals the retired manifest
// script's (test/sandbox/remote/scripts-sh.test.ts pins that script's hash).

const LONG = `déjà vu/${"n".repeat(120)}`;
const FILES: readonly (readonly [string, string, number])[] = [
  ["a", "hello", 0o755],
  ["empty", "", 0o600],
  ["sub/日本.txt", "nihon", 0o644],
  [`sub/${LONG}`, "long", 0o640],
];

function tree(): string {
  const root = mkdtempSync(join(tmpdir(), "threads-tree-"));
  for (const [path, body, mode] of FILES) {
    mkdirSync(join(root, path, ".."), { recursive: true });
    writeFileSync(join(root, path), body);
    chmodSync(join(root, path), mode);
  }
  linkSync(join(root, "a"), join(root, "sub/hard"));
  symlinkSync("../a", join(root, "sub/sym"));
  return root;
}

async function stored(root: string) {
  const tar = Bun.spawn(["tar", "-cf", "-", "-C", root, "."], {
    stdout: "pipe",
    // macOS tar would add AppleDouble `._` entries for extended metadata.
    env: { ...process.env, COPYFILE_DISABLE: "1" },
  });
  const result = await storeTar(tar.stdout, memoryArtifacts());
  expect(await tar.exited).toBe(0);
  if (!result.ok) throw new Error(result.error.message);
  return result.value;
}

const utf8 = new TextEncoder();
const file = (path: string, body: string, mode: number): TreeEntry => ({
  path,
  kind: "file",
  mode,
  size: utf8.encode(body).length,
  sha256: sha256Hex(body),
});

describe.skipIf(Bun.which("tar") === null)("the host's tar", () => {
  test("reads as the tree on disk, the hardlink expanded", async () => {
    const { tree: read } = await stored(tree());
    const want: TreeEntry[] = [
      ...FILES.map(([path, body, mode]) => file(path, body, mode)),
      file("sub/hard", "hello", 0o755),
      { path: "sub", kind: "dir", mode: expect.any(Number) },
      { path: "sub/déjà vu", kind: "dir", mode: expect.any(Number) },
      { path: "sub/sym", kind: "symlink", target: "../a" },
    ];
    expect(read.entries).toEqual(
      want.toSorted((a, b) => (a.path < b.path ? -1 : 1)),
    );
  });
});
