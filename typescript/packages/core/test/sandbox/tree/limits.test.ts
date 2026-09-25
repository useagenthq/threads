import { expect, test } from "bun:test";
import { buildTar } from "../../../src/sandbox/tree/build";
import { readTar } from "../../../src/sandbox/tree/tar";
import { parseTree, type Tree } from "../../../src/sandbox/tree/tree";
import { memoryArtifacts } from "../../../src/store/artifacts";

// The review's attacks: a deep pax path must be refused in linear time, and the builder must
// refuse a tree that breaks the tree rules, wherever the tree came from.

const utf8 = new TextEncoder();

function paxArchive(path: string): Uint8Array {
  // By hand, since the builder refuses such a path: a pax header, a file header, the end.
  const record = (key: string, value: string): string => {
    const body = 3 + key.length + utf8.encode(value).length;
    let length = body + String(body).length;
    if (String(length).length > String(body).length) length += 1;
    return `${length} ${key}=${value}\n`;
  };
  const header = (name: string, type: string, size: number): Uint8Array => {
    const block = new Uint8Array(512);
    const put = (at: number, text: string): void =>
      block.set(utf8.encode(text), at);
    put(0, name);
    put(100, "0000644\0");
    put(108, "0000000\0");
    put(116, "0000000\0");
    put(124, `${size.toString(8).padStart(11, "0")}\0`);
    put(136, "00000000000\0");
    put(156, type);
    put(257, "ustar\u000000");
    block.fill(0x20, 148, 156);
    const sum = block.reduce((n, b) => n + b, 0);
    put(148, `${sum.toString(8).padStart(6, "0")}\0 `);
    return block;
  };
  const pax = utf8.encode(record("path", path));
  const padded = new Uint8Array(Math.ceil(pax.length / 512) * 512);
  padded.set(pax);
  return new Uint8Array([
    ...header("././@PaxHeader", "x", pax.length),
    ...padded,
    ...header("f", "0", 0),
    ...new Uint8Array(1024),
  ]);
}

test("a 40,000-component pax path is refused as bad_path in well under a second", async () => {
  const path = `${"a/".repeat(40_000)}x`;
  const started = performance.now();
  const read = await readTar(
    (async function* () {
      yield paxArchive(path);
    })(),
    memoryArtifacts().sink,
  );
  expect(performance.now() - started).toBeLessThan(250);
  expect(read.ok ? undefined : read.error.reason).toBe("bad_path");
});

test("a 40,000-component tree path is refused in well under a second", () => {
  const tree = `{"entries":[{"kind":"dir","mode":493,"path":"${"a/".repeat(40_000)}x"}],"tree_version":1}`;
  const started = performance.now();
  const parsed = parseTree(utf8.encode(tree));
  expect(performance.now() - started).toBeLessThan(250);
  expect(parsed.ok ? undefined : parsed.error.code).toBe("artifact_corrupt");
});

test("the builder refuses a tree that breaks the tree rules", async () => {
  const bad: readonly Tree[] = [
    {
      tree_version: 1,
      entries: [{ path: "../evil", kind: "dir", mode: 0o755 }],
    },
    {
      tree_version: 1,
      entries: [{ path: "l", kind: "symlink", target: "/etc" }],
    },
    {
      tree_version: 1,
      entries: [
        { path: "l", kind: "symlink", target: "d" },
        { path: "l/x", kind: "dir", mode: 0o755 },
      ],
    },
  ];
  for (const tree of bad) {
    const chunks: Uint8Array[] = [];
    const built = await buildTar(
      tree,
      memoryArtifacts(),
      { uid: 1000, gid: 1000 },
      (c) => chunks.push(c),
    );
    expect(built.ok ? undefined : built.error.code).toBe("artifact_corrupt");
    expect(chunks).toEqual([]);
  }
});
