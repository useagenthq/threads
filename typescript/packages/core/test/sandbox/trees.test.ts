import { describe, expect, test } from "bun:test";
import { err, ok } from "../../src/result";
import { fakeSandbox } from "../../src/sandbox";
import type { ExecOutput, Trees } from "../../src/sandbox/protocol";
import { hashOnly, placeTree, readTree } from "../../src/sandbox/trees";
import { memoryArtifacts } from "../../src/store/artifacts";
import { unwrap } from "../store/helpers";
import { CTX } from "./context";
import { sampleTree, trees } from "./remote/trees";

// Core's side of the Trees capability: placeTree on the in-memory fake, and every export read
// as the sandbox response it is.

async function* bytes(...chunks: Uint8Array[]): AsyncIterable<Uint8Array> {
  yield* chunks;
}

/** A session whose export answers `output`; its import is never called. */
function exporting(output: () => ExecOutput): Trees {
  return {
    exportTree: async () => ok(output()),
    importTree: () => {
      throw new Error("not imported");
    },
  };
}

describe("placeTree on the fake", () => {
  test("a 0755 file and a symlink survive, a setuid bit doesn't", async () => {
    const box = unwrap(await fakeSandbox().create("op", CTX));
    const artifacts = memoryArtifacts();
    const tree = await sampleTree(artifacts);
    expect(await placeTree(trees(box), tree, artifacts, CTX)).toEqual(
      ok(undefined),
    );
    const read = unwrap(await readTree(trees(box), CTX, hashOnly));
    expect(
      read.entries.map((e) =>
        e.kind === "file" ? [e.path, e.mode] : [e.path],
      ),
    ).toEqual([["bin/run", 0o755], ["bin/su", 0o755], ["link"]]);
  });

  test("a /workspace that isn't empty is refused", async () => {
    const box = unwrap(await fakeSandbox().create("op", CTX));
    unwrap(await box.upload("/workspace/a", new Uint8Array([1]), CTX));
    const placed = await placeTree(
      trees(box),
      { tree_version: 1, entries: [] },
      memoryArtifacts(),
      CTX,
    );
    expect(placed.ok ? undefined : placed.error.code).toBe(
      "workspace_not_empty",
    );
  });

  test("a re-export that differs is tree_mismatch", async () => {
    const box = trees(unwrap(await fakeSandbox().create("op", CTX)));
    const artifacts = memoryArtifacts();
    // An import that lands nothing: the re-export is empty.
    const lossy: Trees = {
      exportTree: box.exportTree,
      importTree: async () => ok(undefined),
    };
    const placed = await placeTree(
      lossy,
      await sampleTree(artifacts),
      artifacts,
      CTX,
    );
    expect(placed.ok ? undefined : placed.error.code).toBe("tree_mismatch");
  });
});

describe("an export is a sandbox response", () => {
  const done =
    (code: number, stdout: Uint8Array, stderr = "") =>
    () => ({
      exit_code: Promise.resolve(code),
      stdout: bytes(stdout),
      stderr: bytes(new TextEncoder().encode(stderr)),
    });

  test("bytes that aren't an archive are archive_invalid", async () => {
    const read = await readTree(
      exporting(done(0, new Uint8Array(512).fill(7))),
      CTX,
      hashOnly,
    );
    expect(read.ok ? undefined : read.error.code).toBe("archive_invalid");
  });

  test("a failed export is unavailable, with what it said", async () => {
    const read = await readTree(
      exporting(done(2, new Uint8Array(1024), "tar: denied")),
      CTX,
      hashOnly,
    );
    expect(read).toEqual(
      err({
        code: "unavailable",
        message: "the export of /workspace exited 2: tar: denied",
      }),
    );
  });

  test("a transport that fails after the archive is unavailable", async () => {
    const read = await readTree(
      exporting(() => ({
        exit_code: Promise.reject(new Error("reset")),
        stdout: bytes(new Uint8Array(1024)),
        stderr: bytes(),
      })),
      CTX,
      hashOnly,
    );
    expect(read.ok ? undefined : read.error.code).toBe("unavailable");
  });
});
