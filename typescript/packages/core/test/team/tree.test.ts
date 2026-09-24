import { describe, expect, test } from "bun:test";
import { treeCost } from "../../src/thread/cost-tree";
import type { VerifiedLog } from "../../src/verify";
import { code, unwrap } from "../store/helpers";
import {
  liftTeamRefusal,
  stagedLogs,
  storeLogs,
  type Team,
  teamStore,
  verified,
} from "./kit";

// Tree cost with teams (spec/schema/README.md, "Tree walks with teams"): a lead's members are
// found from its team_members rows and counted after their backlink is checked. The priced
// totals are pinned in both languages (python/tests/team), so the walks agree byte for byte.

liftTeamRefusal();

const POLICY = {
  currency: "USD",
  models: [
    {
      provider: "scripted",
      name: "scripted-1",
      context_window: 200_000,
      max_output_tokens: 1024,
      input_billing_bound: "context_window",
      price: { input: 3000, output: 15_000 },
    },
  ],
};

type Line = Record<string, unknown>;
const data = (line: Line): Line => Object(line["data"]);

/** Pins POLICY in the thread_started of every log in `priced`. */
const pricing =
  (priced: readonly string[]) =>
  (label: string, line: Line): Line =>
    line["type"] === "thread_started" && priced.includes(label)
      ? { ...line, data: { ...data(line), policy: POLICY } }
      : line;

function lead(t: Team): VerifiedLog {
  const log = t.logs.get("lead");
  if (log === undefined) throw new Error("no lead log");
  return log;
}

function cost(t: Team) {
  const at = lead(t);
  const thread = at.segments[0]?.header.thread_id;
  if (thread === undefined) throw new Error("no header");
  return treeCost(t.store, thread, at);
}

const SETTLE = "team-settle-wakes-lead";

describe("tree cost walks a lead's members", () => {
  test("adds a member from its own log", () => {
    const t = teamStore(
      stagedLogs(
        SETTLE,
        ["lead", "researcher", "team"],
        pricing(["lead", "researcher"]),
      ),
    );
    expect(unwrap(cost(t))).toEqual({
      currency: "USD",
      known_nanos: 1_800_000,
      upper_bound_nanos: 1_800_000,
      complete: true,
      bounded: true,
    });
  });

  test("an unpriced member that ran makes the total incomplete", () => {
    const t = teamStore(
      stagedLogs(SETTLE, ["lead", "researcher", "team"], pricing(["lead"])),
    );
    expect(unwrap(cost(t))).toEqual({
      currency: "USD",
      known_nanos: 1_380_000,
      upper_bound_nanos: 1_380_000,
      complete: false,
      bounded: false,
    });
  });

  test("a member in the starting window counts zero and leaves the total complete", () => {
    const t = teamStore(
      stagedLogs(
        "team-tree-starting-member-pending",
        ["lead", "team"],
        pricing(["lead"]),
      ),
    );
    expect(unwrap(cost(t))).toEqual({
      currency: "USD",
      known_nanos: 960_000,
      upper_bound_nanos: 960_000,
      complete: true,
      bounded: true,
    });
  });

  test("a missing branch after the task notification is log_corrupt", () => {
    const t = teamStore(
      stagedLogs("team-tree-missing-branch-after-notice-rejected", [
        "lead",
        "team",
      ]),
    );
    expect(code(cost(t))).toBe("log_corrupt");
  });

  test("a forged backlink is log_corrupt", () => {
    const t = teamStore(stagedLogs(SETTLE, ["lead", "researcher", "team"]));
    // Replace the researcher's stored log with one whose parent names another event.
    const forged = stagedLogs(SETTLE, ["researcher"], (_, line) =>
      line["type"] === "thread_started"
        ? {
            ...line,
            data: {
              ...data(line),
              parent: {
                ...Object(data(line)["parent"]),
                event_id: "0192e001-0000-7000-8000-0000000000ff",
              },
            },
          }
        : line,
    );
    const branch = t.logs.get("researcher")?.segments[0]?.header.branch_id;
    for (const table of ["events", "branches"])
      t.db.run(`DELETE FROM ${table} WHERE branch_id = ?`, [branch ?? ""]);
    storeLogs(t.store, [
      verified(forged.get("researcher") ?? new Uint8Array()),
    ]);
    const failed = cost(t);
    expect(failed.ok ? "ok" : failed.error.message).toContain(
      "doesn't name the member_started",
    );
  });
});
