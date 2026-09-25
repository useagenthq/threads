import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import {
  type ArtifactStore,
  LogStore,
  memoryArtifacts,
  type Store,
} from "@threads/core";
import { openBunSqlite } from "@threads/core/bun-sqlite";
import {
  openStore,
  type StoreDriver,
  sha256Hex,
  storeOf,
} from "@threads/core/host";
import { z } from "zod";
import type { Golden } from "./goldens";

// A store holding golden logs, whose branches can be cut back and grown one stored line at a
// time: another writer's appends, as the exporter sees them.

export type Harness = {
  readonly store: Store;
  readonly db: StoreDriver;
  readonly clock: { now: number };
};

export async function harness(): Promise<Harness> {
  const clock = { now: 1_790_000_100_000 };
  const now = (): number => clock.now;
  const db = openBunSqlite(":memory:");
  const artifacts = memoryArtifacts();
  const log = await LogStore.open(db, now, artifacts);
  if (!log.ok) throw new Error(log.error.message);
  return {
    store: storeOf({ log: log.value, artifacts }, { db, now }),
    db,
    clock,
  };
}

/** Imports a golden's branches (parents first) with the artifacts their requests name. */
export async function importGolden(h: Harness, g: Golden): Promise<void> {
  const { artifacts, log } = await openedOf(h);
  const dir = join(g.dir, "artifacts");
  for (const name of safeList(dir))
    await artifacts.put(readFileSync(join(dir, name)));
  for (const b of g.branches) {
    const imported = await log.importLog(
      readFileSync(join(g.dir, `${b}.jsonl`)),
    );
    if (!imported.ok)
      throw new Error(`${g.name}/${b}: ${imported.error.message}`);
  }
}

function openedOf(h: Harness): Promise<{
  readonly artifacts: ArtifactStore;
  readonly log: LogStore;
}> {
  return openStore(h.store);
}

function safeList(dir: string): readonly string[] {
  try {
    return readdirSync(dir);
  } catch {
    return [];
  }
}

type Row = {
  readonly seq: number;
  readonly event_id: string;
  readonly type: string;
  readonly type_version: number;
  readonly critical: number;
  readonly epoch: number;
  readonly line: Uint8Array;
};

const Row: z.ZodType<Row> = z.object({
  seq: z.int(),
  event_id: z.string(),
  type: z.string(),
  type_version: z.int(),
  critical: z.int(),
  epoch: z.int(),
  line: z.instanceof(Uint8Array),
});

/** Cuts the branch's own rows back to seq `k`; returns them, to grow back in order. */
export function cut(
  h: Harness,
  branchId: string,
  k: number,
): Promise<readonly Row[]> {
  return h.db.transaction(async (tx) => {
    const rows = z
      .array(Row)
      .parse(
        await tx.all(
          "SELECT seq, event_id, type, type_version, critical, epoch, line FROM events WHERE branch_id = ? AND seq > ? ORDER BY seq",
          [branchId, k],
        ),
      );
    await tx.run("DELETE FROM events WHERE branch_id = ? AND seq > ?", [
      branchId,
      k,
    ]);
    const last = z
      .array(z.object({ line: z.instanceof(Uint8Array) }))
      .parse(
        await tx.all(
          "SELECT line FROM events WHERE branch_id = ? AND seq = ? UNION ALL SELECT header_line FROM branches WHERE branch_id = ?",
          [branchId, k, branchId],
        ),
      )[0];
    await tx.run(
      "UPDATE branches SET head_seq = ?, head_hash = ? WHERE branch_id = ?",
      [k, sha256Hex(last?.line ?? new Uint8Array()), branchId],
    );
    return rows;
  });
}

/** Appends one cut row back, moving the head as an append's transaction does. */
export async function grow(
  h: Harness,
  branchId: string,
  row: Row,
): Promise<void> {
  await h.db.transaction(async (tx) => {
    await tx.run(
      "INSERT INTO events (branch_id, seq, event_id, type, type_version, critical, epoch, line) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
      [
        branchId,
        row.seq,
        row.event_id,
        row.type,
        row.type_version,
        row.critical,
        row.epoch,
        row.line,
      ],
    );
    await tx.run(
      "UPDATE branches SET head_seq = ?, head_hash = ? WHERE branch_id = ?",
      [row.seq, sha256Hex(row.line), branchId],
    );
  });
}
