import { expect } from "bun:test";
import { z } from "zod";
import { type BranchId, TeamId, type ThreadId } from "../../src/log";
import { knownEvents, reduce } from "../../src/reduce";
import type { LogStore } from "../../src/store";
import { checkTeamLogs } from "../../src/team/cross";
import { teamMembers } from "../../src/team/members";
import { rebuildTeamIndex } from "../../src/team/rebuild";
import { type VerifiedLog, verifyExport } from "../../src/verify";
import { fixture } from "../store/helpers";
import { storeLogs, teamIndexRows } from "../team/kit";
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

/** A lead among the logs, and the team it leads. */
type Lead = Labelled & { readonly team: TeamId };

function leadsOf(logs: readonly Labelled[]): readonly Lead[] {
  return logs.flatMap((l) => {
    const started = knownEvents(l.log).find((e) => e.type === "thread_started");
    const team =
      started?.type === "thread_started" ? started.data.team?.id : undefined;
    return team === undefined ? [] : [{ ...l, team }];
  });
}

/** The tenant the team is indexed under: its lead principal's, as team_opened records it. */
function tenantOf(logs: readonly Labelled[]): string {
  for (const l of logs)
    for (const e of knownEvents(l.log))
      if (e.type === "team_opened") return e.data.lead.tenant;
  throw new Error("no team log among the logs");
}

const Row = z.strictObject({
  team_id: TeamId,
  thread_id: z.string(),
  name: z.string(),
  generation: z.int(),
  role: z.enum(["lead", "member"]),
});

/**
 * The tree as cost and usage walk it, over every team_members row in key order: a lead's row is
 * where its walk starts (counted), a member's is counted or pending as its lead's walk finds it.
 */
function tree(
  store: LogStore,
  leads: readonly Lead[],
): { counted: string[]; pending: string[] } | Failure {
  const counted: string[] = [];
  const pending: string[] = [];
  const rows = z
    .array(Row)
    .parse(
      store.driver.all(
        "SELECT team_id, name, generation, role, thread_id FROM team_members ORDER BY team_id, name, generation",
        [],
      ),
    );
  const seen = new Set<string>();
  for (const row of rows) {
    // A nested lead has two rows and one thread: counted once.
    if (seen.has(row.thread_id)) continue;
    seen.add(row.thread_id);
    if (row.role === "lead") {
      counted.push(row.name);
      continue;
    }
    const lead = leads.find((l) => l.team === row.team_id);
    if (lead === undefined) throw new Error(`no lead of ${row.team_id}`);
    const members = teamMembers(store, lead.log);
    if (!members.ok) return failure(members.error, lead.label);
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
  const { store, db } = fixture(tenantOf(logs));
  storeLogs(
    store,
    logs.map((l) => l.log),
  );
  const leads = leadsOf(logs);
  for (const lead of leads) {
    const rebuilt = rebuildTeamIndex(store, lead.team);
    if (!rebuilt.ok) return failure(rebuilt.error, lead.label);
  }
  const index = teamIndexRows(
    db,
    leads.map((l) => l.team),
    logs.map((l) => l.branchId),
  );
  const walked = tree(store, leads);
  db.close();
  return "code" in walked ? walked : { states, index, tree: walked };
}

/** Runs one `team` case and compares its outcome. */
export function runTeam(c: Case): void {
  const got = run(c);
  if (c.error !== undefined) {
    expect(plain(got)).toEqual(plain(c.error));
    return;
  }
  if ("code" in got) throw new Error(`${got.code} at ${got.log}@${got.seq}`);
  expect(plain(got.states)).toEqual(plain(c.team.states));
  expect(plain(got.index)).toEqual(plain(c.team.index));
  expect(plain(got.tree)).toEqual(plain(c.team.tree));
}
