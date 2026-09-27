import { describe, expect, test } from "bun:test";
import { z } from "zod";
import { takeMail } from "../../src/agent/team/units";
import { BranchId, canonicalize, type KnownEvent } from "../../src/log";
import { knownEvents } from "../../src/reduce";
import { ok } from "../../src/result";
import type { LogStore, StoreDriver } from "../../src/store";
import { openBunSqlite } from "../../src/store/bun-sqlite";
import { IMPL } from "../../src/store/writer";
import { Batch } from "../../src/team/batch";
import { hostTeamIds } from "../../src/team/host-team";
import { turnProvenance } from "../../src/team/provenance";
import { rebuildTeamIndex } from "../../src/team/rebuild";
import { askRow, memberRows } from "../../src/team/rows";
import { settle } from "../../src/team/settle";
import { VERSION } from "../../src/version";
import { type Fixture, fixture, unwrap } from "../store/helpers";
import { Crash, crashing, type Point } from "./crash-kit";
import {
  assertTeamReplays,
  CASES,
  caseLog,
  exec,
  reading,
  relinked,
  storeLogs,
  TENANT,
  verified,
} from "./kit";

// The Teams Phase 2 crash drills (lane 29D) around a host member's mail: a kill at each commit
// point, then a restart that finishes the work exactly once. The lazy open's own drills are in
// host-open-drills.test.ts.
//
// 1. A caller's ask is committed and the host is killed inside billing's consume: the rebuild finds
//    it through the caller's log alone (D.4).
// 2. billing's reply is committed and the host is killed inside the caller's consume: the caller's
//    park resumes once, with one tool_result.
// 3. A hop cap is exhausted mid-turn and the process is killed after the turn's ending append:
//    billing is back to idle, the ask is failed, and there is no restart and no member_ended.
// 4. A host member's end bounces the ask its turn had taken (coordinator decision 6).
//
// Every one of them ends in the replay check, and every assertion reads log evidence only: no
// pre-commit order and no created_at.

const IDS = hostTeamIds(TENANT);
const CALLER = BranchId.parse("0192b000-0000-7000-8000-0000000000d1");
const BILLING = BranchId.parse("0192b000-0000-7000-8000-0000000000c1");
const ASK = `${CALLER}:c1`;
const LABELS: readonly string[] = ["team", "billing", "support"];
/** The clock every host team case describes; the ask it opens is due at 1_790_000_600_000. */
const NOW = 1_790_000_060_000;

const CONSUMED: Point = {
  name: "a mail row's consume",
  at: (sql) => sql.includes("UPDATE mail SET state"),
};

/** The logs on a store at the case's clock, with the host team index built from them. */
async function seeded(
  logs: ReadonlyMap<string, Uint8Array>,
  driver?: StoreDriver,
): Promise<Fixture> {
  const fx = await fixture(TENANT, driver);
  fx.clock.now = NOW;
  await storeLogs(fx.store, [...logs.values()].map(verified));
  unwrap(await rebuildTeamIndex(fx.store, IDS.teamId));
  return fx;
}

const utf8 = new TextDecoder();
const bytesOf = new TextEncoder();

/**
 * A case log under this implementation's own header, re-chained: the corpus is written by
 * threads-py, and only the impl a header names may append to that branch. `keep` cuts the log back
 * to its first events, as a kill before the later ones would have left it.
 */
function ownLog(bytes: Uint8Array, keep?: number): Uint8Array {
  const [header = "", ...rest] = utf8.decode(bytes).split("\n");
  const own = unwrap(
    canonicalize(
      z.json().parse({
        ...z.record(z.string(), z.json()).parse(JSON.parse(header)),
        writer: { impl: IMPL, version: VERSION },
      }),
    ),
  );
  const events = rest
    .filter((l) => l !== "" && !l.includes('"threads.head"'))
    .slice(0, keep);
  return relinked(bytesOf.encode([own, ...events].join("\n")), (line) => line);
}

/** The case's three logs, each optionally cut back to its first `keep[label]` events. */
function logsOf(
  name: string,
  keep: ReadonlyMap<string, number> = new Map(),
): ReadonlyMap<string, Uint8Array> {
  return new Map(
    LABELS.map((label) => [
      label,
      ownLog(caseLog(name, label), keep.get(label)),
    ]),
  );
}

const typesOf = async (
  log: LogStore,
  branch: BranchId,
): Promise<readonly string[]> =>
  knownEvents(unwrap(await log.read(branch))).map((e) => e.type);

/** A second store over the same database, whose driver dies once at `point`. */
async function dying(fx: Fixture, point: Point): Promise<LogStore> {
  const bad = await fixture(TENANT, crashing(fx.db, point));
  bad.clock.now = NOW;
  return bad.store;
}

/** takeMail's environment over a store: the store's own log and artifacts. */
const mailEnv = (log: LogStore) => ({ log, artifacts: log.artifacts });

describe("a kill before billing's consume", () => {
  const CASE = "host-caller-ask-pending-only-in-caller-log";

  test("leaves the ask only in the caller's log, and the rebuild finds it there", async () => {
    // The caller's ask is committed; the host dies inside billing's consume, so nothing of it
    // lands. Only the caller's log records the ask, and the rebuild finds it there (D.4).
    const fx = await seeded(logsOf(CASE), openBunSqlite(":memory:"));
    const bad = await dying(fx, CONSUMED);
    await expect(takeMail(mailEnv(bad), BILLING)).rejects.toThrow(Crash);
    expect(await typesOf(fx.store, BILLING)).toEqual(["thread_started"]);
    // A fresh process wipes and refolds: the ask comes back through the caller's log.
    unwrap(await rebuildTeamIndex(fx.store, IDS.teamId));
    const row = await reading(fx.db, (tx) => askRow(tx, ASK));
    expect(row?.state).toBe("open");
    expect(row?.asker_branch_id).toBe(CALLER);
    await assertTeamReplays(fx.store, IDS.teamId);
  });

  test("the rebuild sees the pending ask through the caller's log alone", async () => {
    const logs = logsOf(CASE);
    const fx = await seeded(logs);
    expect((await reading(fx.db, (tx) => askRow(tx, ASK)))?.state).toBe("open");
    // Without the caller's log nothing names the ask: billing never consumed it, so the caller
    // scan is the only way the rebuild can see it.
    const without = new Map(logs);
    without.delete("support");
    const bare = await seeded(without);
    expect(await reading(bare.db, (tx) => askRow(tx, ASK))).toBeUndefined();
    await assertTeamReplays(fx.store, IDS.teamId);
  });
});

const RESUMED: readonly string[] = [
  "message_received",
  "ask_closed",
  "resumed",
  "tool_result",
];

describe("a kill inside the caller's consume", () => {
  const CASE = "host-caller-reply-unconsumed";

  test("leaves the caller's park to resume exactly once", async () => {
    // billing's reply is committed; the host dies inside the caller's consume. Nothing of that
    // append lands, and after the restart the caller takes it once.
    const fx = await seeded(logsOf(CASE), openBunSqlite(":memory:"));
    const before = await typesOf(fx.store, CALLER);
    const bad = await dying(fx, CONSUMED);
    await expect(takeMail(mailEnv(bad), CALLER)).rejects.toThrow(Crash);
    expect(await typesOf(fx.store, CALLER)).toEqual(before);
    // The dead process's lease outlives it until its TTL; a restart waits that out.
    await exec(fx.db, "UPDATE leases SET expires_at = 0");
    const taken = await takeMail(mailEnv(fx.store), CALLER);
    expect(taken?.status).toBe("consumed");
    expect((await typesOf(fx.store, CALLER)).slice(before.length)).toEqual([
      ...RESUMED,
    ]);
    await assertTeamReplays(fx.store, IDS.teamId);
  });

  test("a second pass takes nothing and records no second result", async () => {
    const fx = await seeded(logsOf(CASE));
    const before = await typesOf(fx.store, CALLER);
    expect(before).not.toContain("ask_closed");
    expect((await takeMail(mailEnv(fx.store), CALLER))?.status).toBe(
      "consumed",
    );
    const after = await typesOf(fx.store, CALLER);
    expect(after.slice(before.length)).toEqual([...RESUMED]);
    // The row moved, so a second pass takes nothing.
    expect((await takeMail(mailEnv(fx.store), CALLER))?.status).toBe(
      "nothing_pending",
    );
    expect(await typesOf(fx.store, CALLER)).toEqual(after);
    expect((await reading(fx.db, (tx) => askRow(tx, ASK)))?.state).toBe(
      "answered",
    );
    await assertTeamReplays(fx.store, IDS.teamId);
  });
});

describe("a hop cap exhausted mid-turn", () => {
  test("leaves the member idle, the ask failed, and no restart", async () => {
    const fx = await seeded(logsOf("host-member-hop-capped"));
    const rows = await reading(fx.db, (tx) => memberRows(tx, IDS.teamId));
    expect(
      rows.map((r) => [String(r.name), r.generation, r.state] as const),
    ).toEqual([["billing", 1, "idle"]]);
    expect((await reading(fx.db, (tx) => askRow(tx, ASK)))?.state).toBe(
      "failed",
    );
    const own = await typesOf(fx.store, BILLING);
    expect(own).toContain("member_idle");
    expect(own).not.toContain("member_ended");
    const log = knownEvents(unwrap(await fx.store.read(IDS.branchId)));
    expect(log.filter((e) => e.type === "member_ended")).toEqual([]);
    // One generation: a failed turn is no reason to supervise or restart.
    expect(
      log.flatMap((e) =>
        e.type === "member_started" ? [e.data.member.generation] : [],
      ),
    ).toEqual([1]);
    await assertTeamReplays(fx.store, IDS.teamId);
  });
});

const ENDED = "host-member-end-bounces-taken-ask";
const BUDGET = {
  limit: "max_cost_nanos",
  limit_value: 5_000_000_000,
  observed: 5_200_000_000,
  observed_is_upper_bound: false,
  scope: "thread",
} as const;
const HOST = {
  type_version: 1,
  critical: true,
  actor: { kind: "host" },
} as const;

/** billing's own thread budget runs out in the turn that took the ask. */
async function endOverBudget(log: LogStore): Promise<void> {
  const writer = unwrap(await log.acquire(BILLING, "drill"));
  const thread = writer.chain.segments[0]?.header.thread_id;
  if (thread === undefined) throw new Error("a writer's chain has a header");
  const appended = await writer.appendDecided(async (tx) => {
    const batch = new Batch(tx.chain.fold.seq, tx.now);
    batch.add({ ...HOST, type: "budget_exceeded", data: BUDGET });
    batch.add({
      ...HOST,
      type: "turn_completed",
      data: { reason: "budget_exhausted" },
    });
    const provenance = await turnProvenance(tx.tx, tx.chain);
    if (provenance === undefined) throw new Error("the ask opened a turn");
    await settle(
      {
        tx: tx.tx,
        batch,
        threadId: thread,
        branchId: BILLING,
        provenance,
        put: () => {
          throw new Error("this drill's result has no text");
        },
        takenAsks: tx.chain.fold.team.host.turnAsks,
      },
      { status: "budget_exhausted", budget: BUDGET },
    );
    return ok(batch.drafts);
  });
  await writer.release();
  if (!("ok" in appended) || !appended.ok)
    throw new Error("a settlement never refuses");
}

describe("a host member's end (coordinator decision 6)", () => {
  test("bounces the ask its turn took, so the asker closes it at once", async () => {
    // The ask was consumed, so the end's refusal of pending mail can never reach it. Both logs are
    // cut back to before the end, so what closes the ask here is this runtime's own appends.
    const fx = await seeded(
      logsOf(
        ENDED,
        new Map([
          ["billing", 2],
          ["support", 9],
        ]),
      ),
    );
    expect(await typesOf(fx.store, BILLING)).toEqual([
      "thread_started",
      "message_received",
    ]);
    const parked = await typesOf(fx.store, CALLER);
    expect(parked.at(-1)).toBe("parked");
    const due = (await reading(fx.db, (tx) => askRow(tx, ASK)))?.deadline ?? 0;
    await endOverBudget(fx.store);
    expect(await typesOf(fx.store, BILLING)).toEqual([
      "thread_started",
      "message_received",
      "budget_exceeded",
      "turn_completed",
      "member_ended",
      "message_sent",
    ]);
    expect(bounces(knownEvents(unwrap(await fx.store.read(BILLING))))).toEqual([
      { code: "member_ended", ask_id: ASK },
    ]);
    expect((await takeMail(mailEnv(fx.store), CALLER))?.status).toBe(
      "consumed",
    );
    expect((await typesOf(fx.store, CALLER)).slice(parked.length)).toEqual([
      ...RESUMED,
    ]);
    expect((await reading(fx.db, (tx) => askRow(tx, ASK)))?.state).toBe(
      "member_ended",
    );
    // At once: the ask closed while its own deadline was still ahead of the append's clock.
    expect(due).toBeGreaterThan(NOW);
    await assertTeamReplays(fx.store, IDS.teamId);
  });
});

/** The bounces a log sent: their code and the ask each answers. */
const bounces = (log: readonly KnownEvent[]) =>
  log.flatMap((e) =>
    e.type === "message_sent" && e.data.envelope.kind === "bounce"
      ? [{ code: e.data.envelope.code, ask_id: String(e.data.envelope.ask_id) }]
      : [],
  );

test("the drill cases are in the corpus", async () => {
  // A case that moves out from under these drills fails here, not silently.
  for (const name of [
    "host-caller-ask-pending-only-in-caller-log",
    "host-caller-reply-unconsumed",
    "host-member-hop-capped",
    ENDED,
  ])
    expect(await Bun.file(`${CASES}/${name}/case.json`).exists(), name).toBe(
      true,
    );
});
