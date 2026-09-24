import { describe, expect, test } from "bun:test";
import { z } from "zod";
import { BranchId, SandboxId } from "../../src/log";
import { fixture, snapshot, unwrap, userInput } from "../store/helpers";
import { assertTeamReplays, caseLogs, TENANT, teamOf, verified } from "./kit";
import { reappend } from "./writes";

// A fork of a lead's or a member's branch shares its thread but is not the team's branch: its
// appends write no team index rows and no feed rows (team fork is deferred), so the index still
// equals a rebuild from the team's own logs.

const SETTLE = "team-settle-wakes-lead";
const LEAD = BranchId.parse("0192b000-0000-7000-8000-0000000000b1");
const MEMBER = BranchId.parse("0192b000-0000-7000-8000-0000000000b2");
const FORK = BranchId.parse("0192b000-0000-7000-8000-0000000000f1");
const Rows = z.array(z.record(z.string(), z.unknown()));

function forkAndAppend(original: BranchId): ReturnType<typeof fixture> {
  const logs = [
    ...caseLogs(SETTLE, ["lead", "researcher", "team"]).values(),
  ].map((b) => verified(b));
  const fx = fixture(TENANT);
  reappend(fx, logs);
  const parent = unwrap(fx.store.acquire(original, "replay"));
  unwrap(parent.append([snapshot(null)]));
  const atSeq = parent.chain.fold.seq;
  parent.release();
  const child = unwrap(
    fx.store.beginFork({
      parent: original,
      atSeq,
      branch: FORK,
      holderId: "forker",
    }),
  );
  unwrap(
    fx.store.finishFork(child, {
      sandboxId: SandboxId.parse("sbx_child_01"),
      knowledgePolicy: "pinned",
    }),
  );
  const forked = unwrap(fx.store.acquire(FORK, "forker"));
  unwrap(forked.append([userInput("Another way.")]));
  return fx;
}

describe("a fork of a team branch", () => {
  for (const [who, branch] of [
    ["lead", LEAD],
    ["member", MEMBER],
  ] as const)
    test(`an append to a fork of the ${who} writes no team rows`, () => {
      const fx = forkAndAppend(branch);
      expect(
        Rows.parse(
          fx.db.all("SELECT * FROM team_feed WHERE branch_id = ?", [FORK]),
        ),
      ).toEqual([]);
      const team = teamOf(
        [...caseLogs(SETTLE, ["lead"]).values()].map((b) => verified(b)),
      );
      assertTeamReplays(fx.store, team);
    });
});
