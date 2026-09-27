import { afterAll, describe, expect, test } from "bun:test";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { z } from "zod";
import { knownEvents } from "../../src/reduce";
import { LogStore, memoryArtifacts, type StoreDriver } from "../../src/store";
import { openBunSqlite } from "../../src/store/bun-sqlite";
import { hostTeamIds } from "../../src/team/host-team";
import { fixture, unwrap } from "../store/helpers";
import { Crash, crashing, type Point } from "./crash-kit";
import { openOn, TENANT } from "./host-open-worker";
import { assertTeamReplays, query } from "./kit";

// The Teams Phase 2 lazy-open drills (lane 29D): a host team's ids are derived, so opening it is
// idempotent and racing it is decided by the branch's primary key.
//
// 1. A crash before the commit leaves no host team; after the commit there is one, and the second
//    open is already_open and writes nothing.
// 2. Two real processes on one store file open it at once: one team_opened and one member_started
//    per configured member, and the loser's open is already_open.
//
// Every assertion reads log evidence only: no pre-commit order and no created_at.

const IDS = hostTeamIds(TENANT);

const TEAMS_ROW: Point = {
  name: "the teams row",
  at: (sql) => sql.startsWith("INSERT INTO teams"),
};

/** The host team log's event types; none at all when its branch was never opened. */
async function opened(log: LogStore): Promise<readonly string[]> {
  const read = await log.read(IDS.branchId);
  return read.ok ? knownEvents(read.value).map((e) => e.type) : [];
}

const Count = z.array(z.strictObject({ n: z.int() }));

async function teamCount(db: StoreDriver): Promise<number> {
  const rows = Count.parse(await query(db, "SELECT COUNT(*) AS n FROM teams"));
  return rows[0]?.n ?? 0;
}

describe("the host team's lazy open", () => {
  test("a crash before its commit leaves no host team", async () => {
    const db = openBunSqlite(":memory:");
    const artifacts = memoryArtifacts();
    const dying = unwrap(
      await LogStore.open(crashing(db, TEAMS_ROW), Date.now, artifacts, TENANT),
    );
    await expect(openOn(dying, "one")).rejects.toThrow(Crash);
    // A fresh process: the whole open rolled back, so nothing of the team is stored.
    const again = unwrap(await LogStore.open(db, Date.now, artifacts, TENANT));
    expect(await teamCount(db)).toBe(0);
    expect(await opened(again)).toEqual([]);
  });

  test("after the commit the second open is already_open and writes nothing", async () => {
    const fx = await fixture(TENANT, openBunSqlite(":memory:"));
    expect((await openOn(fx.store, "one")).ok).toBe(true);
    expect(await opened(fx.store)).toEqual(["team_opened", "member_started"]);
    // A second process at the same derived ids: the branch exists, so its open is a no-op.
    expect((await openOn(fx.store, "two")).ok).toBe(true);
    expect(await opened(fx.store)).toEqual(["team_opened", "member_started"]);
    expect(await teamCount(fx.db)).toBe(1);
    await assertTeamReplays(fx.store, IDS.teamId);
  });
});

const dir = mkdtempSync(join(tmpdir(), "threads-host-open-"));
afterAll(() => rmSync(dir, { recursive: true, force: true }));

const GO = "go";
const WORKER = join(import.meta.dir, "host-open-worker.ts");

/**
 * A real second process on the same store file: the race is decided by the primary key on the
 * derived branch, which only two connections can contend for.
 */
function other(path: string) {
  const proc = Bun.spawn(["bun", WORKER, path, GO], {
    stdin: "pipe",
    stdout: "pipe",
    stderr: "inherit",
  });
  const reader = proc.stdout.getReader();
  const decoder = new TextDecoder();
  return {
    line: async (): Promise<string> => {
      const { value, done } = await reader.read();
      if (done || value === undefined) throw new Error("the other side exited");
      return decoder.decode(value).trim();
    },
    go: (): void => {
      proc.stdin.write(`${GO}\n`);
      proc.stdin.flush();
    },
    kill: (): void => proc.kill(),
  };
}

describe("two processes racing the open", () => {
  test("one team_opened and one member_started per configured member", async () => {
    const path = join(dir, "race.db");
    const proc = other(path);
    try {
      const fx = await fixture(TENANT, openBunSqlite(path));
      expect(await proc.line()).toBe("ready");
      proc.go();
      expect((await openOn(fx.store, "one")).ok).toBe(true);
      expect(await proc.line()).toBe("opened");
      // Both opens carry the same derived ids, so the loser collided on the branch's primary key
      // and got already_open: the log is the only evidence of who wrote, and it holds one of each.
      const log = knownEvents(unwrap(await fx.store.read(IDS.branchId)));
      expect(log.filter((e) => e.type === "team_opened")).toHaveLength(1);
      expect(
        log.flatMap((e) =>
          e.type === "member_started" ? [String(e.data.member.name)] : [],
        ),
      ).toEqual(["billing"]);
      await assertTeamReplays(fx.store, IDS.teamId);
    } finally {
      proc.kill();
    }
  });
});
