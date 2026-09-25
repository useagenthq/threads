import { z } from "zod";
import type { RunResult, TeamAgent } from "../../src";
import { hostRunner } from "../../src/agent/registry";
import { type Store, storeOf } from "../../src/agent/sqlite";
import {
  type BranchId,
  type TeamId,
  TeamId as TeamIdSchema,
  ThreadId,
} from "../../src/log";
import {
  LogStore,
  memoryArtifacts,
  type SqlValue,
  type StoreDriver,
  type Tx,
} from "../../src/store";
import { openBunSqlite } from "../../src/store/bun-sqlite";
import { unwrap } from "../store/helpers";
import { query } from "./kit";

// Shared by the team crash drills: a driver that dies once inside one commit point's transaction,
// and the restart, the host's recovery of the lead's open run with no new input.

export class Crash extends Error {}

export type Point = {
  readonly name: string;
  readonly at: (sql: string, params: readonly SqlValue[]) => boolean;
};

const utf8 = new TextDecoder();

/** A statement parameter holds `text` (a string, or JSON bytes). */
export const mentions = (params: readonly SqlValue[], text: string): boolean =>
  params.some(
    (p) =>
      (typeof p === "string" && p.includes(text)) ||
      (p instanceof Uint8Array && utf8.decode(p).includes(text)),
  );

/**
 * The same database, through a driver that dies once at `point`: that transaction rolls back and
 * the run rejects. Work still in flight may go on, as a sibling process's would.
 */
export function crashing(base: StoreDriver, point: Point): StoreDriver {
  let crashed = false;
  const wrap = (tx: Tx): Tx => ({
    ...tx,
    run: (sql, params = []) => {
      if (!crashed && point.at(sql, params)) {
        crashed = true;
        throw new Crash(`killed at ${point.name}`);
      }
      return tx.run(sql, params);
    },
    transaction: (fn) => tx.transaction((inner) => fn(wrap(inner))),
  });
  return {
    ...base,
    transaction: (fn, options) =>
      base.transaction((tx) => fn(wrap(tx)), options),
  };
}

const Row = z.object({ thread_id: ThreadId, team_id: z.string() });

/** A drill's database, its log store, and the host's restart of the lead. */
export type Drill = {
  readonly db: StoreDriver;
  readonly log: LogStore;
  /** The store over the drill's database, through `driver`. */
  readonly open: (driver: StoreDriver) => Promise<Store>;
  readonly restart: (lead: TeamAgent) => Promise<{
    readonly result: RunResult<unknown>;
    readonly branch: BranchId;
    readonly team: TeamId;
  }>;
};

/** One database, opened as a store through any driver over it. */
export async function drill(): Promise<Drill> {
  const db = openBunSqlite(":memory:");
  const artifacts = memoryArtifacts();
  const log = unwrap(await LogStore.open(db, Date.now, artifacts));
  const open = async (driver: StoreDriver): Promise<Store> =>
    storeOf({
      log: unwrap(await LogStore.open(driver, Date.now, artifacts)),
      artifacts,
    });
  /**
   * The host's recovery of the lead's open run, with no new input, once the dead process's leases
   * have expired.
   */
  const restart: Drill["restart"] = async (lead) => {
    await db.transaction((tx) =>
      tx.run("UPDATE leases SET expires_at = 0", []),
    );
    const [row] = z
      .array(Row)
      .parse(
        await query(
          db,
          "SELECT lead_thread_id AS thread_id, team_id FROM teams",
          [],
        ),
      );
    if (row === undefined)
      throw new Error("the lead's first append opened its team");
    const store = await open(db);
    const branch = unwrap(await log.mainBranch(row.thread_id));
    const runner = hostRunner(lead);
    if (runner === undefined)
      throw new Error("agent() registers a host runner");
    const result = await runner.execute(
      {
        store,
        principal: { issuer: "api", tenant: "local", subject: "operator" },
        thread: { id: row.thread_id, branch, store },
      },
      [],
    );
    return {
      result,
      branch,
      team: TeamIdSchema.parse(row.team_id),
    };
  };
  return { db, log, open, restart };
}
