import { expect, test } from "bun:test";
import { buildTar } from "../../../src/sandbox/tree/build";
import { readTar } from "../../../src/sandbox/tree/tar";
import {
  encodeTree,
  parseTree,
  type Tree,
  type TreeEntry,
} from "../../../src/sandbox/tree/tree";
import { memoryArtifacts } from "../../../src/store/artifacts";
import { chunked, seeded } from "./chunks";

// Randomized properties: a random tree survives build then read; any corruption of a valid
// archive is a value (never a throw) and the same value for every chunking.

const NAMES = ["a", "b", "é", "\u{1f600}", "", "a b", "x\\y", "n".repeat(90)];

function pick<T>(random: () => number, items: readonly T[]): T {
  const item = items[Math.floor(random() * items.length)];
  if (item === undefined) throw new Error("empty");
  return item;
}

/** A random valid tree whose file bytes are in `artifacts`. */
async function randomTree(
  random: () => number,
  artifacts: ReturnType<typeof memoryArtifacts>,
): Promise<Tree> {
  const entries = new Map<string, TreeEntry>();
  const dirs = [""];
  for (let i = 0; i < 1 + random() * 12; i++) {
    const parent = pick(random, dirs);
    const path =
      parent === "" ? pick(random, NAMES) : `${parent}/${pick(random, NAMES)}`;
    if (entries.has(path)) continue;
    const roll = random();
    if (roll < 0.3) {
      entries.set(path, {
        path,
        kind: "dir",
        mode: 0o700 + Math.floor(random() * 0o100),
      });
      dirs.push(path);
    } else if (roll < 0.45) {
      entries.set(path, { path, kind: "symlink", target: pick(random, NAMES) });
    } else {
      const bytes = new Uint8Array(Math.floor(random() * 1500)).map(
        () => random() * 256,
      );
      const sha256 = await artifacts.put(bytes);
      const mode = Math.floor(random() * 0o10000);
      entries.set(path, {
        path,
        kind: "file",
        mode,
        size: bytes.length,
        sha256,
      });
    }
  }
  return {
    tree_version: 1,
    entries: [...entries.values()].toSorted((a, b) =>
      a.path < b.path ? -1 : 1,
    ),
  };
}

async function archive(
  tree: Tree,
  artifacts: ReturnType<typeof memoryArtifacts>,
): Promise<Uint8Array> {
  const parts: number[] = [];
  const built = await buildTar(tree, artifacts, { uid: 1000, gid: 1000 }, (b) =>
    parts.push(...b),
  );
  if (!built.ok) throw new Error(built.error.message);
  return new Uint8Array(parts);
}

test("a random tree survives build, then read from any chunking", async () => {
  for (let seed = 1; seed <= 200; seed++) {
    const random = seeded(seed);
    const artifacts = memoryArtifacts();
    const tree = await randomTree(random, artifacts);
    expect(parseTree(encodeTree(tree))).toEqual({ ok: true, value: tree });
    const read = await readTar(
      chunked(await archive(tree, artifacts), random, 900),
      artifacts.sink,
    );
    expect(read).toEqual({ ok: true, value: tree });
  }
});

test("a corrupted archive reads the same from every chunking, and never throws", async () => {
  for (let seed = 1; seed <= 300; seed++) {
    const random = seeded(seed);
    const artifacts = memoryArtifacts();
    const bytes = await archive(await randomTree(random, artifacts), artifacts);
    for (let n = 0; n < 1 + random() * 4; n++)
      bytes[Math.floor(random() * bytes.length)] = Math.floor(random() * 256);
    const cut =
      random() < 0.3
        ? bytes.subarray(0, Math.floor(random() * bytes.length))
        : bytes;
    const whole = await readTar(
      chunked(cut, () => 0.999, cut.length + 1),
      memoryArtifacts().sink,
    );
    const pieces = await readTar(
      chunked(cut, random, 64),
      memoryArtifacts().sink,
    );
    expect(pieces).toEqual(whole);
  }
});
