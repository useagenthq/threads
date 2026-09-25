import { describe, expect } from "bun:test";
import { z } from "zod";
import { sweepArtifacts } from "../../core/src/store/retention";
import { pgArtifacts } from "../src/artifacts";
import { pgTest } from "./kit";
import { pgFixture, T0 } from "./store-kit";

// Artifacts in the database: re-hashed on every read, shared by every machine, and kept by gc
// through their grace window, which a re-put refreshes.

const bytes = (text: string): Uint8Array => new TextEncoder().encode(text);

describe("artifacts on Postgres", () => {
  pgTest(
    "a changed row is artifact_corrupt, a deleted one artifact_missing",
    async () => {
      const f = await pgFixture();
      try {
        const artifacts = pgArtifacts(f.db);
        const sha = await artifacts.put(bytes("spilled"));
        expect((await artifacts.get(sha)).ok).toBe(true);
        await f.db.transaction((tx) =>
          tx.run("UPDATE artifacts SET bytes = ? WHERE sha256 = ?", [
            bytes("changed"),
            sha,
          ]),
        );
        const corrupt = await artifacts.get(sha);
        expect(corrupt.ok ? "ok" : corrupt.error.code).toBe("artifact_corrupt");
        await f.db.transaction((tx) =>
          tx.run("DELETE FROM artifacts WHERE sha256 = ?", [sha]),
        );
        const missing = await artifacts.get(sha);
        expect(missing.ok ? "ok" : missing.error.code).toBe("artifact_missing");
      } finally {
        await f.close();
      }
    },
  );

  pgTest("a second machine reads what the first put", async () => {
    const f = await pgFixture();
    try {
      const other = await f.peer();
      const sha = await pgArtifacts(f.db).put(bytes("a spilled result"));
      const got = await pgArtifacts(other.db).get(sha);
      expect(got.ok ? new TextDecoder().decode(got.value) : got).toBe(
        "a spilled result",
      );
    } finally {
      await f.close();
    }
  });

  pgTest(
    "gc keeps an artifact inside its grace window, re-put included",
    async () => {
      const f = await pgFixture();
      try {
        const clock = { now: T0 };
        const a = pgArtifacts(f.db, () => clock.now);
        const other = await f.peer();
        const sha = await a.put(bytes("put, its append not yet committed"));
        // Machine B sweeps with a window that covers A's put: kept.
        expect(
          await sweepArtifacts(other.db, pgArtifacts(other.db), T0),
        ).toEqual([]);
        // A re-put refreshes created_at: a sweep of the old window keeps it.
        clock.now = T0 + 10_000;
        await a.put(bytes("put, its append not yet committed"));
        const at = z
          .array(z.object({ created_at: z.number() }))
          .parse(
            await f.db.transaction((tx) =>
              tx.all("SELECT created_at FROM artifacts WHERE sha256 = ?", [
                sha,
              ]),
            ),
          );
        expect(at).toEqual([{ created_at: T0 + 10_000 }]);
        expect(
          await sweepArtifacts(other.db, pgArtifacts(other.db), T0 + 5_000),
        ).toEqual([]);
        // The window expired and nothing names it: deleted.
        expect(
          await sweepArtifacts(other.db, pgArtifacts(other.db), T0 + 20_000),
        ).toEqual([sha]);
        const gone = await a.get(sha);
        expect(gone.ok ? "ok" : gone.error.code).toBe("artifact_missing");
      } finally {
        await f.close();
      }
    },
  );
});
