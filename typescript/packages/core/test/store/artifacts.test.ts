import { describe, expect, test } from "bun:test";
import { mkdtempSync, readdirSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileArtifacts, memoryArtifacts } from "../../src/store";
import { StoreError } from "../../src/store/driver";

const bytes = new TextEncoder().encode("dropped bytes");

describe("file artifacts", () => {
  test("round-trip, content-addressed, private", async () => {
    const root = mkdtempSync(join(tmpdir(), "threads-artifacts-"));
    const store = fileArtifacts(root);
    const sha = await store.put(bytes);
    expect(await store.put(bytes)).toBe(sha);
    expect(await store.get(sha)).toEqual({ ok: true, value: bytes });
    const dir = join(root, "sha256", sha.slice(0, 2));
    expect(statSync(dir).mode & 0o777).toBe(0o700);
    expect(statSync(join(dir, sha)).mode & 0o777).toBe(0o600);
  });

  test("a missing or changed artifact is a typed error", async () => {
    const root = mkdtempSync(join(tmpdir(), "threads-artifacts-"));
    const store = fileArtifacts(root);
    const missing = await store.get("0".repeat(64));
    expect(missing.ok ? "ok" : missing.error.code).toBe("artifact_missing");
    const sha = await store.put(bytes);
    writeFileSync(join(root, "sha256", sha.slice(0, 2), sha), "tampered");
    const corrupt = await store.get(sha);
    expect(corrupt.ok ? "ok" : corrupt.error.code).toBe("artifact_corrupt");
  });

  test("a disk that fails is a StoreError; missing and corrupt stay values", async () => {
    const dir = mkdtempSync(join(tmpdir(), "threads-artifacts-"));
    // The root is a file: every read and write under it fails at the file system.
    const root = join(dir, "artifacts");
    writeFileSync(root, "");
    const store = fileArtifacts(root);
    await expect(store.put(bytes)).rejects.toThrow(StoreError);
    await expect(store.get("0".repeat(64))).rejects.toThrow(StoreError);
    expect(() => store.sink()).toThrow(StoreError);
  });
});

describe("artifact sinks", () => {
  const stores = {
    file: () => fileArtifacts(mkdtempSync(join(tmpdir(), "threads-sink-"))),
    memory: memoryArtifacts,
  };
  for (const [name, make] of Object.entries(stores))
    test(`${name}: chunks land as one content-addressed artifact`, async () => {
      const store = make();
      const sink = store.sink();
      sink.write(bytes.subarray(0, 4));
      sink.write(bytes.subarray(4));
      const done = await sink.finish();
      expect(done).toEqual({
        sha256: await store.put(bytes),
        bytes: bytes.length,
      });
      expect(await store.get(done.sha256)).toEqual({ ok: true, value: bytes });
    });

  test("an aborted file sink leaves no temp file", () => {
    const root = mkdtempSync(join(tmpdir(), "threads-sink-"));
    const sink = fileArtifacts(root).sink();
    sink.write(bytes);
    sink.abort();
    expect(readdirSync(join(root, "sha256"))).toEqual([]);
  });
});
