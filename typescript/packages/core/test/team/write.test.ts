import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { z } from "zod";
import { BranchId, type KnownEvent, ThreadId } from "../../src/log";
import { knownEvents } from "../../src/reduce";
import type { Writer } from "../../src/store";
import { ALREADY_OPEN } from "../../src/store/open";
import { claimMail } from "../../src/team/claim";
import { TEAM_CONSTANTS } from "../../src/team/constants";
import { plain } from "../conformance/cases";
import { ENGINE } from "../store/engine";
import { type Fixture, fixture, unwrap } from "../store/helpers";
import {
  assertTeamReplays,
  caseLog,
  exec,
  query,
  TENANT,
  teamOf,
  verified,
} from "./kit";
import { draftOf } from "./writes";

// The index hooks on a live writer: a lead's first append opens its team whole, a failing hook
// rolls the append back, every appended event gets one feed row, and mail.claim's CAS.

const lead = knownEvents(verified(caseLog("team-settle-wakes-lead", "lead")));
const TEAM = teamOf([verified(caseLog("team-settle-wakes-lead", "lead"))]);
const LEAD_THREAD = ThreadId.parse("0192a000-0000-7000-8000-0000000000b1");
const LEAD_BRANCH = BranchId.parse("0192b000-0000-7000-8000-0000000000b1");
const TEAM_LOG = BranchId.parse("0192b000-0000-7000-8000-0000000000b3");

const Rows = z.array(z.record(z.string(), z.unknown()));
const rows = async (fx: Fixture, sql: string): Promise<unknown> =>
  Rows.parse(await query(fx.db, sql, []));

/** The lead's log opened with its first `n` recorded events. */
async function openLead(fx: Fixture, n: number): Promise<Writer> {
  const events: readonly KnownEvent[] = lead.slice(0, n);
  const writer = unwrap(
    await fx.store.openBranch({
      threadId: LEAD_THREAD,
      branchId: LEAD_BRANCH,
      lease: { holderId: "lead", ttlMs: 30_000 },
      drafts: events.map((e) => draftOf(e)),
    }),
  );
  if (writer === ALREADY_OPEN) throw new Error("a new lead");
  return writer;
}

describe("a lead's first append", () => {
  test("opens the team log, the teams row and the lead's row, and the feed starts with team_opened", async () => {
    const fx = await fixture(TENANT);
    await openLead(fx, 2);
    const log = unwrap(await fx.store.read(TEAM_LOG));
    // The thread the lead's thread_started.team names.
    expect(log.segments[0]?.header.thread_id).toBe(
      ThreadId.parse("0192a000-0000-7000-8000-0000000000b3"),
    );
    expect(plain(knownEvents(log).map((e) => [e.type, e.data]))).toEqual([
      [
        "team_opened",
        {
          team: TEAM,
          lead: { tenant: TENANT, team: TEAM, name: "lead", generation: 1 },
          lead_thread_id: LEAD_THREAD,
        },
      ],
    ]);
    expect(
      await rows(
        fx,
        "SELECT team_id, tenant_id, lead_thread_id, team_log_branch_id, closed_at FROM teams",
      ),
    ).toEqual([
      {
        team_id: TEAM,
        tenant_id: TENANT,
        lead_thread_id: LEAD_THREAD,
        team_log_branch_id: TEAM_LOG,
        closed_at: null,
      },
    ]);
    expect(
      await rows(fx, "SELECT name, generation, role, state FROM team_members"),
    ).toEqual([
      { name: "lead", generation: 1, role: "lead", state: "running" },
    ]);
    expect(
      await rows(
        fx,
        "SELECT feed_offset, branch_id, seq FROM team_feed ORDER BY feed_offset",
      ),
    ).toEqual([
      { feed_offset: 1, branch_id: TEAM_LOG, seq: 1 },
      { feed_offset: 2, branch_id: LEAD_BRANCH, seq: 1 },
      { feed_offset: 3, branch_id: LEAD_BRANCH, seq: 2 },
    ]);
    // The team log's lease is free: the next writer takes it at once, at epoch 2.
    expect(
      unwrap(await fx.store.acquire(TEAM_LOG, "operator")).lease.epoch,
    ).toBe(2);
    await assertTeamReplays(fx.store, TEAM);
  });

  test("a team log that already exists refuses the append, and none of it is written", async () => {
    const fx = await fixture(TENANT);
    unwrap(
      await fx.store.createBranch(
        ThreadId.parse("0192a000-0000-7000-8000-0000000000b3"),
        TEAM_LOG,
      ),
    );
    const refused = await fx.store.openBranch({
      threadId: LEAD_THREAD,
      branchId: LEAD_BRANCH,
      lease: { holderId: "lead", ttlMs: 30_000 },
      drafts: lead.slice(0, 2).map((e) => draftOf(e)),
    });
    expect(refused.ok ? "ok" : refused.error.code).toBe("invalid_transition");
    for (const table of ["teams", "team_members", "team_feed"])
      expect(await rows(fx, `SELECT * FROM ${table}`)).toEqual([]);
    expect((await fx.store.branchState(LEAD_BRANCH)).ok).toBe(false);
  });
});

describe("the feed", () => {
  test("gets one row per appended event, offsets in commit order", async () => {
    const fx = await fixture(TENANT);
    const writer = await openLead(fx, 2);
    for (const e of lead.slice(2, 10))
      unwrap(await writer.append([draftOf(e)]));
    const feed = Rows.parse(
      await query(
        fx.db,
        "SELECT feed_offset, seq FROM team_feed WHERE branch_id = ? ORDER BY feed_offset",
        [LEAD_BRANCH],
      ),
    );
    expect(feed.map((r) => r["seq"])).toEqual([1, 2, 3, 4, 5, 6, 7, 8, 9, 10]);
    expect(feed.map((r) => r["feed_offset"])).toEqual([
      2, 3, 4, 5, 6, 7, 8, 9, 10, 11,
    ]);
    await assertTeamReplays(fx.store, TEAM);
  });
});

describe("the lookup every append makes", () => {
  // EXPLAIN QUERY PLAN is SQLite's; the Postgres schema carries the same indexes.
  test.skipIf(ENGINE !== "sqlite")(
    "which team a branch belongs to is found through indexes, never a scan",
    async () => {
      const fx = await fixture(TENANT);
      const plan = Rows.parse(
        await query(
          fx.db,
          `EXPLAIN QUERY PLAN SELECT team_id FROM team_members WHERE thread_id = ? AND branch_id = ?
          UNION SELECT team_id FROM teams WHERE team_log_branch_id = ?`,
          ["t", "b", "b"],
        ),
      ).map((r) => String(r["detail"]));
      expect(plan.some((d) => d.includes("team_members_thread"))).toBe(true);
      expect(plan.some((d) => d.includes("teams_log_branch"))).toBe(true);
      expect(plan.some((d) => d.startsWith("SCAN"))).toBe(false);
    },
  );
});

describe("mail.claim", () => {
  const TASK = "0192b000-0000-7000-8000-0000000000b1:c1";

  test("one worker claims a pending row until its claim expires", async () => {
    const fx = await fixture(TENANT);
    await openLead(fx, 9);
    const t = fx.clock.now;
    expect(await claimMail(fx.db, TASK, "a", t)).toBe("claimed");
    expect(await claimMail(fx.db, TASK, "b", t + 1)).toBe("taken");
    const expiry = t + TEAM_CONSTANTS.claimTtlMs;
    expect(await claimMail(fx.db, TASK, "b", expiry - 1)).toBe("taken");
    expect(await claimMail(fx.db, TASK, "b", expiry, 10)).toBe("claimed");
    expect(
      await rows(fx, "SELECT claim_token, claim_expires_at FROM mail"),
    ).toEqual([{ claim_token: "b", claim_expires_at: expiry + 10 }]);
  });

  test("a row that is not pending, or doesn't exist, is never claimed", async () => {
    const fx = await fixture(TENANT);
    await openLead(fx, 9);
    await exec(fx.db, "UPDATE mail SET state = 'consumed'", []);
    expect(await claimMail(fx.db, TASK, "a", fx.clock.now)).toBe("not_pending");
    expect(await claimMail(fx.db, "no-such-mail", "a", fx.clock.now)).toBe(
      "not_found",
    );
  });
});

describe("the Teams constants", () => {
  test("are the ones the op vectors pin", () => {
    const vectors = z
      .object({ constants: z.record(z.string(), z.number()) })
      .parse(
        JSON.parse(
          readFileSync(
            join(
              import.meta.dir,
              "../../../../../spec/conformance/vectors/team-ops.json",
            ),
            "utf8",
          ),
        ),
      );
    expect(vectors.constants).toEqual({
      inline_cap_bytes: TEAM_CONSTANTS.inlineCapBytes,
      busy_bound_ms: TEAM_CONSTANTS.busyBoundMs,
      claim_ttl_ms: TEAM_CONSTANTS.claimTtlMs,
      ask_wait_default_ms: TEAM_CONSTANTS.askWaitDefaultMs,
      wake_poll_in_process_ms: TEAM_CONSTANTS.wakePollInProcessMs,
      wake_poll_cross_process_ms: TEAM_CONSTANTS.wakePollCrossProcessMs,
    });
  });
});
