import { describe, expect, test } from "bun:test";
import { mkdtempSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileArtifacts } from "../../src/store";

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
