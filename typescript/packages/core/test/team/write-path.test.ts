import { describe, expect, test } from "bun:test";
import { CASE_NAMES, loadCase, plain } from "../conformance/cases";
import { fixture } from "../store/helpers";
import {
  assertTeamReplays,
  STAGED,
  stagedNames,
  TENANT,
  teamIndexRows,
  teamsOf,
  verified,
} from "./kit";
import { reappend } from "./writes";

// Write path equals rebuild path: every recorded team (corpus and staged) appended again event
// by event through writers into a fresh store leaves exactly the index rows the case expects,
// and a wipe and rebuild of each team leaves them again.

const recorded = [
  ...CASE_NAMES.map((name) => loadCase(name)),
  ...stagedNames().map((name) => loadCase(name, STAGED)),
].filter(
  (c) =>
    c.kind === "team" && c.error === undefined && c.team.index !== undefined,
);

describe("the team index on the write path", () => {
  test("covers every recorded team", () => {
    expect(recorded.length).toBeGreaterThanOrEqual(6);
  });

  for (const c of recorded)
    test(`${c.name}: appends leave the expected rows, and a rebuild leaves them again`, async () => {
      const logs = [...c.logs.values()].map((bytes) => verified(bytes));
      const fx = await fixture(TENANT);
      await reappend(fx, logs);
      const teams = teamsOf(logs);
      const branches = logs.flatMap(
        (l) => l.segments[0]?.header.branch_id ?? [],
      );
      const rows = async () =>
        plain(await teamIndexRows(fx.db, teams, branches));
      expect(await rows()).toEqual(plain(c.team.index));
      for (const team of teams) await assertTeamReplays(fx.store, team);
      expect(await rows()).toEqual(plain(c.team.index));
    });
});
