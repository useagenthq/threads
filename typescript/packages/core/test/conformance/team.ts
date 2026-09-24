import { expect } from "bun:test";
import { z } from "zod";
import type { BranchId, ThreadId } from "../../src/log";
import { knownEvents, reduce } from "../../src/reduce";
import type { LogStore } from "../../src/store";
import { checkTeamLogs } from "../../src/team/cross";
import { teamMembers } from "../../src/team/members";
import { rebuildTeamIndex } from "../../src/team/rebuild";
import { type VerifiedLog, verifyExport } from "../../src/verify";
import { fixture } from "../store/helpers";
import { storeLogs, teamIndexRows, teamOf } from "../team/kit";
import { type Case, plain } from "./cases";

// The `team` kind (spec/conformance/README.md): import and reduce every log, check rule 43
// across them, fold the index into a fresh store from the logs alone, then walk the lead's tree.

type Failure = { code: string; seq?: number; log?: string };
type Found = {
  readonly states: Record<string, unknown>;
  readonly index: unknown;
  readonly tree: unknown;
};
type Labelled = {
  readonly label: string;
  readonly log: VerifiedLog;
  readonly threadId: ThreadId;
  readonly branchId: BranchId;
};

function failure(
  error: { readonly code: string; readonly seq?: number | undefined },
  log: string | undefined,
): Failure {
  const out: Failure = { code: error.code };
  if (error.seq !== undefined) out.seq = error.seq;
  if (log !== undefined) out.log = log;
  return out;
}

/** Step 1: every log imported read-only, in input.logs order; the first failure is the result. */
function imported(c: Case): readonly Labelled[] | Failure {
  const out: Labelled[] = [];
  for (const [label, bytes] of c.logs) {
    const read = verifyExport(bytes);
    if (!read.ok) return failure(read.error, label);
    const header = read.value.segments.at(-1)?.header;
    if (header === undefined) throw new Error("a verified log has a header");
    out.push({
      label,
      log: read.value,
      threadId: header.thread_id,
      branchId: header.branch_id,
    });
  }
  return out;
}

/** The tree as cost and usage walk it: the lead's row counted, each member counted or pending. */
function tree(
  store: LogStore,
  logs: readonly Labelled[],
): { counted: string[]; pending: string[] } | Failure {
  const lead = logs.find((l) =>
    knownEvents(l.log).some(
      (e) => e.type === "thread_started" && e.data.team !== undefined,
    ),
  );
  if (lead === undefined) throw new Error("no lead among the logs");
  const members = teamMembers(store, lead.log);
  if (!members.ok) return failure(members.error, lead.label);
  const counted: string[] = [];
  const pending: string[] = [];
  const rows = z
    .array(Row)
    .parse(
      store.driver.all(
        "SELECT name, generation, role FROM team_members WHERE team_id = ? ORDER BY name, generation",
        [teamOf(logs.map((l) => l.log))],
      ),
    );
  for (const row of rows) {
    // The lead's own row is where the walk starts: counted, never a child.
    if (row.role === "lead") {
      counted.push(row.name);
      continue;
    }
    const member = members.value.find(
      (m) => m.name === row.name && m.generation === row.generation,
    );
    if (member === undefined) throw new Error(`no member ${row.name}`);
    const open = member.open();
    if (!open.ok) return failure(open.error, lead.label);
    (open.value === "pending" ? pending : counted).push(row.name);
  }
  return { counted, pending };
}

const Row = z.strictObject({
  name: z.string(),
  generation: z.int(),
  role: z.enum(["lead", "member"]),
});

function run(c: Case): Found | Failure {
  const logs = imported(c);
  if ("code" in logs) return logs;
  const states = Object.fromEntries(
    logs.map((l) => [l.label, plain(reduce(l.log, c.now))]),
  );
  const broken = checkTeamLogs(
    logs.map((l) => ({ ...l, events: knownEvents(l.log) })),
  );
  if (broken !== undefined)
    return failure(
      { code: "invalid_transition", seq: broken.seq },
      logs.find((l) => l.branchId === broken.branchId)?.label,
    );
  const { store, db } = fixture();
  storeLogs(
    store,
    logs.map((l) => l.log),
  );
  const team = teamOf(logs.map((l) => l.log));
  const rebuilt = rebuildTeamIndex(store, team);
  if (!rebuilt.ok) return failure(rebuilt.error, undefined);
  const index = teamIndexRows(
    db,
    team,
    logs.map((l) => l.branchId),
  );
  const walked = tree(store, logs);
  db.close();
  return "code" in walked ? walked : { states, index, tree: walked };
}

/**
 * Runs one `team` case and compares its outcome. `states: false` skips the reduced states,
 * which for mail-opened turns need lane 21A's reducer (the staged direct test only).
 */
export function runTeam(
  c: Case,
  { states = true }: { readonly states?: boolean } = {},
): void {
  const got = run(c);
  if (c.error !== undefined) {
    expect(plain(got)).toEqual(plain(c.error));
    return;
  }
  if ("code" in got) throw new Error(`${got.code} at ${got.log}@${got.seq}`);
  if (states) expect(plain(got.states)).toEqual(plain(c.team.states));
  expect(plain(got.index)).toEqual(plain(c.team.index));
  expect(plain(got.tree)).toEqual(plain(c.team.tree));
}
