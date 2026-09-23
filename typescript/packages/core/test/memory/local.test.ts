import { describe, expect, test } from "bun:test";
import { ConfigError } from "../../src/agent/errors";
import { sha256Hex } from "../../src/hash";
import {
  knowledgeProviderSuite,
  memoryProviderSuite,
} from "../../src/memory/conformance";
import {
  bindLocalKnowledge,
  localKnowledge,
} from "../../src/memory/local-knowledge";
import { bindMemory, localMemory } from "../../src/memory/local-memory";
import type { Scope } from "../../src/memory/protocol";
import { memoryArtifacts, type SqliteDriver } from "../../src/store";
import { openBunSqlite } from "../../src/store/bun-sqlite";
import { unwrap } from "../store/helpers";

// The built-in providers: the shared provider suites, then what only they promise (FTS5 setup
// check, rebuildable index, parse failures).

const S: Scope = { tenant_id: "t", agent: "a", scope: "u" };
const encoder = new TextEncoder();

function knowledge() {
  const found = bindLocalKnowledge(
    localKnowledge({ paths: [] }),
    openBunSqlite(":memory:"),
    memoryArtifacts(),
  );
  if (found === undefined) throw new Error("localKnowledge binds");
  return found.local;
}

const runner = {
  test: (name: string, body: () => Promise<void>) => test(name, body),
};
describe("provider suite", () => {
  memoryProviderSuite(
    "localMemory",
    async () => bindMemory(localMemory(), openBunSqlite(":memory:")),
    runner,
  );
  knowledgeProviderSuite("localKnowledge", async () => knowledge(), runner);
});

describe("localMemory", () => {
  test("unbound, it is a typed unavailable error, never a throw", async () => {
    const got = await localMemory().recall(S, "x");
    expect(got.ok ? "ok" : got.error.code).toBe("unavailable");
  });

  test("a SQLite without FTS5 is a setup error that names FTS5", () => {
    const db = openBunSqlite(":memory:");
    const noFts: SqliteDriver = {
      ...db,
      exec: (sql) => {
        if (sql.includes("fts5")) throw new Error("no such module: fts5");
        db.exec(sql);
      },
    };
    let thrown: unknown;
    try {
      bindMemory(localMemory(), noFts);
    } catch (error) {
      thrown = error;
    }
    expect(thrown).toBeInstanceOf(ConfigError);
    expect(thrown instanceof ConfigError && thrown.code).toBe(
      "capability_missing",
    );
    expect(String(thrown)).toContain("FTS5");
  });

  test("query text can't form FTS5 syntax", async () => {
    const p = bindMemory(localMemory(), openBunSqlite(":memory:"));
    const hits = unwrap(await p.recall(S, 'text: OR "x" NEAR(*) -'));
    expect(hits).toEqual([]);
  });
});

describe("localKnowledge", () => {
  const src = (text: string, id = "doc.md") => {
    const content = encoder.encode(text);
    return {
      source: {
        source_id: id,
        media_type: "text/markdown",
        content,
        binding: { namespace: "n", record_id: `${id}-${sha256Hex(content)}` },
      },
      key: `${id}@${sha256Hex(content)}`,
    };
  };

  test("the index rebuilds from admitted versions with the same results (F14.3)", async () => {
    const p = knowledge();
    for (const [i, t] of [
      "alpha beta\n\ngamma",
      "beta delta",
      "epsilon",
    ].entries()) {
      const s = src(t, `d${i}.md`);
      unwrap(await p.ingest(S, s.source, s.key));
    }
    const before = unwrap(await p.search(S, "beta"));
    unwrap(p.rebuild());
    expect(unwrap(await p.search(S, "beta"))).toEqual(before);
    expect(before.length).toBe(2);
  });

  test("bytes that aren't UTF-8 text are an explicit parse error (F14.7)", async () => {
    const p = knowledge();
    const got = await p.ingest(
      S,
      {
        source_id: "bin.txt",
        media_type: "text/plain",
        content: Uint8Array.from([0xff, 0xfe, 0x00]),
        binding: { namespace: "n", record_id: "r" },
      },
      "k",
    );
    expect(got.ok ? "ok" : got.error.code).toBe("invalid");
    expect(unwrap(await p.search(S, "bin"))).toEqual([]);
  });

  test("the same key with other content is invalid; with the same content a no-op", async () => {
    const p = knowledge();
    const s = src("one");
    const v = unwrap(await p.ingest(S, s.source, s.key));
    expect(unwrap(await p.ingest(S, s.source, s.key))).toEqual(v);
    const other = src("two");
    const got = await p.ingest(S, other.source, s.key);
    expect(got.ok ? "ok" : got.error.code).toBe("invalid");
  });

  test("sources narrows the search; passages tile a long document with exact spans", async () => {
    const p = knowledge();
    const long = Array.from(
      { length: 40 },
      (_, i) => `Paragraph ${i} about refunds é.\n\n`,
    ).join("");
    const a = src(long, "long.md");
    const b = src("refunds elsewhere", "other.md");
    unwrap(await p.ingest(S, a.source, a.key));
    unwrap(await p.ingest(S, b.source, b.key));
    const hits = unwrap(
      await p.search(S, "refunds", { k: 20, sources: ["long.md"] }),
    );
    expect(hits.length).toBeGreaterThan(1);
    for (const h of hits) {
      expect(h.doc_id).toBe("long.md");
      expect(
        new TextDecoder().decode(
          a.source.content.subarray(h.span.start, h.span.end),
        ),
      ).toBe(h.text);
    }
  });
});
