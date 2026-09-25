import { expect } from "bun:test";
import { ConfigError } from "@threads/core/adapter";
import { localKnowledge } from "../../core/src/memory";
import { bindLocalKnowledge } from "../../core/src/memory/local-knowledge";
import { bindMemory, localMemory } from "../../core/src/memory/local-memory";
import { pgArtifacts } from "../src/artifacts";
import { pgTest } from "./kit";
import { pgFixture } from "./store-kit";

// What a Postgres store refuses: the SQLite-only providers (a missing URL: factory.test.ts).

pgTest(
  "localMemory() and localKnowledge() are refused on a Postgres store",
  async () => {
    const f = await pgFixture();
    try {
      const memory = await bindMemory(localMemory(), f.db).catch(
        (error: unknown) => error,
      );
      expect(memory).toBeInstanceOf(ConfigError);
      expect(String(memory)).toContain("use supermemory() or zep()");
      const knowledge = await bindLocalKnowledge(
        localKnowledge({ paths: [] }),
        f.db,
        pgArtifacts(f.db),
      ).catch((error: unknown) => error);
      expect(knowledge).toBeInstanceOf(ConfigError);
      expect(String(knowledge)).toContain("run this agent on sqlite()");
    } finally {
      await f.close();
    }
  },
);
