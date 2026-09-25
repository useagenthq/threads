import { describe, expect, test } from "bun:test";
import { z } from "zod";
import { BranchId, ThreadId } from "../../src/log";
import { deleteThread } from "../../src/store/deletion";
import { materialize } from "../../src/team/materialize";
import { code, unwrap } from "../store/helpers";
import { query } from "./kit";
import { DOC, seeded, TEAM, worldLogs } from "./vectors";

// Materialize versus delete (design §4.15), with a barrier between materialize's prework (the
// rebind) and its write transaction. Delete first: the row check inside the transaction finds
// the member gone and opens nothing. Materialize first: the member's branch holds a live first
// lease, so the delete is refused busy and writes nothing. Never an orphan branch.

const VECTOR = DOC.vectors.find((v) => v.name === "materialize-opens-branch");
if (VECTOR === undefined)
  throw new Error("the op vectors have materialize-opens-branch");
const vector = VECTOR;
const MEMBER_BRANCH = BranchId.parse("0192b000-0000-7000-8000-0000000000b2");
const Count = z.array(z.object({ n: z.int() }));

function lead(): ThreadId {
  const log = worldLogs(vector)["lead"];
  if (log === undefined) throw new Error("the world has a lead");
  return ThreadId.parse(log.thread_id);
}

describe("materialize racing a delete", () => {
  test("delete first: the row check finds the member gone and nothing is opened", async () => {
    const fx = await seeded(vector);
    const got = unwrap(
      await materialize(fx.store, TEAM, "researcher-1", {
        artifacts: fx.artifacts,
        // The barrier: the lead is deleted after the prework, before the write transaction.
        rebind: async () => {
          unwrap(await deleteThread(fx.db, "acme", lead(), fx.clock.now));
          return { status: "ok" };
        },
        holder: "worker",
        ttlMs: 30_000,
        branchId: MEMBER_BRANCH,
      }),
    );
    expect(got.status).toBe("not_starting");
    const branches = Count.parse(
      await query(fx.db, "SELECT COUNT(*) AS n FROM branches", []),
    )[0]?.n;
    expect(branches).toBe(0);
    expect(code(await fx.store.branchState(MEMBER_BRANCH))).toBe(
      "branch_not_found",
    );
  });

  test("materialize first: the member's live lease makes the delete busy", async () => {
    const fx = await seeded(vector);
    const got = unwrap(
      await materialize(fx.store, TEAM, "researcher-1", {
        artifacts: fx.artifacts,
        rebind: async () => ({ status: "ok" }),
        holder: "worker",
        ttlMs: 30_000,
        branchId: MEMBER_BRANCH,
      }),
    );
    expect(got.status).toBe("materialized");
    const before = Count.parse(
      await query(fx.db, "SELECT COUNT(*) AS n FROM events", []),
    )[0]?.n;
    expect(code(await deleteThread(fx.db, "acme", lead(), fx.clock.now))).toBe(
      "busy",
    );
    expect(
      Count.parse(await query(fx.db, "SELECT COUNT(*) AS n FROM events", []))[0]
        ?.n,
    ).toBe(before);
    expect(await fx.store.branchState(MEMBER_BRANCH)).toEqual({
      ok: true,
      value: "ready",
    });
  });
});
