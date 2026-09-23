import { describe, expect, test } from "bun:test";
import { mkdtempSync, readdirSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileArtifacts, memoryArtifacts } from "../../src/store";

const bytes = new TextEncoder().encode("dropped bytes");

describe("file artifacts", () => {
  test("round-trip, content-addressed, private", () => {
    const root = mkdtempSync(join(tmpdir(), "threads-artifacts-"));
    const store = fileArtifacts(root);
    const sha = store.put(bytes);
    expect(store.put(bytes)).toBe(sha);
    expect(store.get(sha)).toEqual({ ok: true, value: bytes });
    const dir = join(root, "sha256", sha.slice(0, 2));
    expect(statSync(dir).mode & 0o777).toBe(0o700);
    expect(statSync(join(dir, sha)).mode & 0o777).toBe(0o600);
  });

  test("a missing or changed artifact is a typed error", () => {
    const root = mkdtempSync(join(tmpdir(), "threads-artifacts-"));
    const store = fileArtifacts(root);
    const missing = store.get("0".repeat(64));
    expect(missing.ok ? "ok" : missing.error.code).toBe("artifact_missing");
    const sha = store.put(bytes);
    writeFileSync(join(root, "sha256", sha.slice(0, 2), sha), "tampered");
    const corrupt = store.get(sha);
    expect(corrupt.ok ? "ok" : corrupt.error.code).toBe("artifact_corrupt");
  });
});

describe("artifact sinks", () => {
  const stores = {
    file: () => fileArtifacts(mkdtempSync(join(tmpdir(), "threads-sink-"))),
    memory: memoryArtifacts,
  };
  for (const [name, make] of Object.entries(stores))
    test(`${name}: chunks land as one content-addressed artifact`, () => {
      const store = make();
      const sink = store.sink();
      sink.write(bytes.subarray(0, 4));
      sink.write(bytes.subarray(4));
      const done = sink.finish();
      expect(done).toEqual({ sha256: store.put(bytes), bytes: bytes.length });
      expect(store.get(done.sha256)).toEqual({ ok: true, value: bytes });
    });

  test("an aborted file sink leaves no temp file", () => {
    const root = mkdtempSync(join(tmpdir(), "threads-sink-"));
    const sink = fileArtifacts(root).sink();
    sink.write(bytes);
    sink.abort();
    expect(readdirSync(join(root, "sha256"))).toEqual([]);
  });
});
