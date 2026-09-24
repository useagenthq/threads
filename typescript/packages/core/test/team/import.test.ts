import { describe, expect, test } from "bun:test";
import { z } from "zod";
import { BranchId, ThreadId } from "../../src/log";
import { ALREADY_OPEN } from "../../src/store/open";
import { caseStore, loadCase, plain } from "../conformance/cases";
import { fixture, unwrap } from "../store/helpers";
import {
  assertTeamReplays,
  caseLog,
  TENANT,
  teamIndexRows,
  teamOf,
  verified,
} from "./kit";
import { draftOf } from "./writes";

// An import stores bytes without its appends' hooks, so it folds the index again in the same
// transaction: the imported branches' wake rows, and every team the log belongs to.

const LEAD = BranchId.parse("0192b000-0000-7000-8000-0000000000b1");
const TEAM_LOG = BranchId.parse("0192b000-0000-7000-8000-0000000000b3");
const Rows = z.array(z.record(z.string(), z.unknown()));

describe("an import folds the index again", () => {
  test("a background child still running has its wake row", () => {
    const c = loadCase("legacy-wake-pending-row");
    const { store, db } = caseStore(c);
    unwrap(store.importLog(c.log ?? new Uint8Array()));
    expect(
      Rows.parse(
        db.all("SELECT branch_id, child_thread_id FROM pending_wakes", []),
      ),
    ).toEqual(Rows.parse(plain(c.projections?.["pending_wakes"])));
  });

  test("a team imported log by log has the rows its appends wrote", () => {
    const lead = verified(caseLog("team-settle-wakes-lead", "lead")).events;
    const team = teamOf([verified(caseLog("team-settle-wakes-lead", "lead"))]);
    const written = fixture(TENANT);
    const writer = unwrap(
      written.store.openBranch({
        threadId: ThreadId.parse("0192a000-0000-7000-8000-0000000000b1"),
        branchId: LEAD,
        lease: { holderId: "lead", ttlMs: 30_000 },
        drafts: lead
          .slice(0, 2)
          .flatMap((l) => (l.kind === "event" ? [draftOf(l.event)] : [])),
      }),
    );
    expect(writer).not.toBe(ALREADY_OPEN);
    const imported = fixture(TENANT);
    for (const branch of [LEAD, TEAM_LOG])
      unwrap(
        imported.store.importLog(unwrap(written.store.exportBranch(branch))),
      );
    const rows = (db: typeof written.db) => {
      const { team_feed: _feed, ...rest } = teamIndexRows(
        db,
        [team],
        [LEAD, TEAM_LOG],
      );
      return rest;
    };
    expect(rows(imported.db)).toEqual(rows(written.db));
    assertTeamReplays(imported.store, team);
  });
});
