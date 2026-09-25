import { describe, expect, test } from "bun:test";
import { z } from "zod";
import { BranchId } from "../../src/log";
import { knownEvents } from "../../src/reduce";
import type { Writer } from "../../src/store";
import { materialize } from "../../src/team/materialize";
import type { Fixture } from "../store/helpers";
import { unwrap } from "../store/helpers";
import { assertTeamReplays } from "./kit";
import { runOn } from "./op-run";
import {
  changes,
  DOC,
  rows,
  seeded,
  TEAM,
  type Vector,
  vectorMint,
  worldLogs,
} from "./vectors";

// The op vectors this build runs (spec/conformance/vectors/team-ops.json): each op on its world
// through this runtime's own store ops reaches the reference's outcome, appends the same event
// types per log and makes the same row changes; the team then replays.

/** A vector tagged for a later lane is not run by this build. */
const runs = (v: Vector): boolean => v.lane === undefined;

async function materializeOp(fx: Fixture, v: Vector): Promise<unknown> {
  const rebind = z
    .enum(["ok", "pin_unavailable", "pin_mismatch", "setup_failed"])
    .parse(v.input["rebind"]);
  const got = unwrap(
    await materialize(fx.store, TEAM, z.string().parse(v.input["member"]), {
      artifacts: fx.artifacts,
      rebind: async () => ({ status: rebind }),
      holder: "vectors",
      ttlMs: 30_000,
      mint: vectorMint,
      branchId: BranchId.parse(v.input["branch_id"]),
    }),
  );
  return got.status === "rebind_failed"
    ? { status: got.status, code: got.code }
    : { status: got.status };
}

async function writerOf(fx: Fixture, v: Vector): Promise<Writer> {
  const log = worldLogs(v)[v.by];
  if (log === undefined) throw new Error(`no log ${v.by}`);
  return unwrap(
    await fx.store.acquire(BranchId.parse(log.branch_id), "vectors"),
  );
}

async function run(fx: Fixture, v: Vector): Promise<unknown> {
  return v.op === "materialize"
    ? materializeOp(fx, v)
    : runOn(await writerOf(fx, v), v);
}

/** The event types appended per log label since `heads`. */
async function appended(
  fx: Fixture,
  v: Vector,
  heads: ReadonlyMap<string, number>,
): Promise<Readonly<Record<string, readonly string[]>>> {
  const out: Record<string, string[]> = {};
  const labels = {
    ...Object.fromEntries(
      Object.entries(worldLogs(v)).map(([k, l]) => [k, l.branch_id]),
    ),
  };
  if (v.op === "materialize")
    labels[z.string().parse(v.input["label"])] = z
      .string()
      .parse(v.input["branch_id"]);
  for (const [label, branch] of Object.entries(labels)) {
    const read = await fx.store.read(BranchId.parse(branch));
    if (!read.ok) continue;
    const types = knownEvents(read.value)
      .filter((e) => e.seq > (heads.get(label) ?? 0))
      .map((e) => e.type);
    if (types.length > 0) out[label] = types;
  }
  return out;
}

describe("team op vectors, run by this runtime", () => {
  const mine = DOC.vectors.filter(runs);

  test("cover every op, the operator's included", () => {
    expect(new Set(mine.map((v) => v.op))).toEqual(
      new Set([
        ...["start", "send", "ask", "reply", "wait", "monitor", "cancel"],
        ...["deadline", "consume"],
        ...["materialize", "idle", "end"],
      ]),
    );
    // Pinned: a vector that drops out of the selection fails here, not silently.
    expect(mine).toHaveLength(103);
    expect(mine.filter((v) => v.by === "team")).toHaveLength(25);
  });

  for (const v of mine)
    test(v.name, async () => {
      const fx = await seeded(v);
      const before = await rows(fx);
      const heads = new Map<string, number>();
      for (const [label, log] of Object.entries(worldLogs(v)))
        heads.set(
          label,
          unwrap(await fx.store.read(BranchId.parse(log.branch_id))).events
            .length,
        );
      expect(await run(fx, v)).toEqual(v.expect.outcome);
      expect(await appended(fx, v, heads)).toEqual(v.expect.appended);
      expect(changes(before, await rows(fx))).toEqual(v.expect.rows);
      await assertTeamReplays(fx.store, TEAM);
    });
});
