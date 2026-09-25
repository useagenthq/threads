import { Database } from "bun:sqlite";
import { describe, expect } from "bun:test";
import { StoreError } from "@threads/core/store-driver";
import { z } from "zod";
import { LogStore, memoryArtifacts } from "../../core/src/store";
import { STORE_SQL as SQLITE_SQL } from "../../core/src/store/generated/sql";
import { openPg } from "../src/driver";
import { STORE_VERSION } from "../src/generated/sql";
import { freshSchema, pgTest } from "./kit";
import { pgFixture, T0 } from "./store-kit";

// The Postgres schema: created once however many machines open it, versioned as SQLite's, byte
// ordered, and the same tables and columns as SQLite's.

const now = (): number => T0;

describe("opening", () => {
  pgTest(
    "eight openers of one empty database: one schema, eight opens, no retry",
    async () => {
      const { url, drop } = await freshSchema();
      const drivers = Array.from({ length: 8 }, () => openPg(url));
      try {
        const opened = await Promise.all(
          drivers.map((db) => LogStore.open(db, now, memoryArtifacts())),
        );
        expect(opened.map((o) => o.ok)).toEqual(Array(8).fill(true));
        expect(drivers.map((d) => d.retries())).toEqual(Array(8).fill(0));
      } finally {
        for (const d of drivers) await d.close();
        await drop();
      }
    },
  );

  pgTest("two schemas open side by side", async () => {
    const [a, b] = await Promise.all([pgFixture(), pgFixture()]);
    await a.close();
    await b.close();
  });

  for (const version of [STORE_VERSION - 1, STORE_VERSION + 1])
    pgTest(`a store of version ${version} is unsupported_format`, async () => {
      const f = await pgFixture();
      try {
        await f.db.transaction((tx) =>
          tx.run(
            "UPDATE threads_meta SET value = ? WHERE key = 'store_version'",
            [String(version)],
          ),
        );
        const again = openPg(f.url);
        const opened = await LogStore.open(again, now, memoryArtifacts());
        await again.close();
        expect(opened.ok ? "ok" : opened.error.code).toBe("unsupported_format");
      } finally {
        await f.close();
      }
    });

  pgTest("the version row is store.sql's version", async () => {
    const f = await pgFixture();
    try {
      const rows = await f.db.transaction((tx) =>
        tx.all("SELECT value FROM threads_meta WHERE key = 'store_version'"),
      );
      expect(rows).toEqual([{ value: String(STORE_VERSION) }]);
    } finally {
      await f.close();
    }
  });
});

const Column = z.object({ table: z.string(), column: z.string() });

describe("the tables", () => {
  pgTest(
    "every store.sql table and column exists on Postgres, text in byte order",
    async () => {
      const f = await pgFixture();
      try {
        const pg = z
          .array(
            Column.extend({
              collation: z.string().nullable(),
              type: z.string(),
            }),
          )
          .parse(
            await f.db.transaction((tx) =>
              tx.all(
                `SELECT table_name AS "table", column_name AS "column",
               collation_name AS collation, data_type AS type
             FROM information_schema.columns WHERE table_schema = current_schema()`,
              ),
            ),
          );
        const lite = new Database(":memory:");
        lite.exec(SQLITE_SQL);
        const tables = z
          .array(z.object({ name: z.string() }))
          .parse(
            lite
              .query("SELECT name FROM sqlite_master WHERE type = 'table'")
              .all(),
          );
        const expected = tables.flatMap(({ name }) =>
          z
            .array(z.object({ name: z.string() }))
            .parse(
              lite.query(`SELECT name FROM pragma_table_info('${name}')`).all(),
            )
            .map((c) => `${name}.${c.name}`),
        );
        const found = new Set(pg.map((c) => `${c.table}.${c.column}`));
        expect(expected.filter((c) => !found.has(c))).toEqual([]);
        const unordered = pg.filter(
          (c) => c.type === "text" && c.collation !== "C",
        );
        expect(unordered).toEqual([]);
      } finally {
        await f.close();
      }
    },
  );

  pgTest(
    "text compares by bytes, whatever the database's collation",
    async () => {
      const f = await pgFixture();
      try {
        const names = ["Zed", "alpha", "émile", "a-10", "a-9"];
        await f.db.transaction(async (tx) => {
          for (const n of names)
            await tx.run(
              "INSERT INTO threads (thread_id, tenant_id) VALUES (?, 'local')",
              [n],
            );
        });
        const rows = z
          .array(z.object({ thread_id: z.string() }))
          .parse(
            await f.db.transaction((tx) =>
              tx.all("SELECT thread_id FROM threads ORDER BY thread_id"),
            ),
          );
        const bytes = (s: string): string =>
          Buffer.from(s, "utf8").toString("latin1");
        const byBytes = names.toSorted((a, b) =>
          bytes(a) < bytes(b) ? -1 : bytes(a) > bytes(b) ? 1 : 0,
        );
        expect(rows.map((r) => r.thread_id)).toEqual(byBytes);
      } finally {
        await f.close();
      }
    },
  );

  pgTest(
    "an int8 past 2^53 - 1 read back is a bug, not a StoreError",
    async () => {
      const f = await pgFixture();
      try {
        const read = f.db.transaction((tx) =>
          tx.all("SELECT CAST(9007199254740993 AS BIGINT) AS n"),
        );
        await expect(read).rejects.toThrow("safe integer range");
        const thrown = await read.catch((error: unknown) => error);
        expect(thrown instanceof StoreError).toBe(false);
      } finally {
        await f.close();
      }
    },
  );
});
