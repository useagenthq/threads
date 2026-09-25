import { describe, expect, test } from "bun:test";
import { caseArtifacts, replayCase } from "../thread/replay";
import { type Fixture, fixture, rows as sqlAll, T0, unwrap } from "./helpers";

// Import derives the approval and question rows from the log's settlement events alone, never
// from the importing store's clock (spec/schema/README.md, "Questions and remembered rules").

const alice = { issuer: "api", tenant: "acme", subject: "alice" };
const HOUR = 3_600_000;
const APPROVAL = "recovery-approval-pending-parks";
const TS_LOG = "log.threads-ts.jsonl";

/** The branch exported and imported into a fresh store whose clock is two hours on. */
async function imported(
  name: string,
  from: Fixture,
  branch: Parameters<Fixture["store"]["exportBranch"]>[0],
): Promise<Fixture> {
  const bytes = unwrap(await from.store.exportBranch(branch));
  const to = await fixture("acme");
  await caseArtifacts(name, to);
  to.clock.now = T0 + 2 * HOUR;
  unwrap(await to.store.importLog(bytes));
  return to;
}

describe("rows derived on import", () => {
  test("an open question imports with an open row", async () => {
    const name = "ask-user-rejected-answer-keeps-question-open";
    const { f, branch } = await replayCase(name, 7);
    const to = await imported(name, f, branch);
    expect(
      await sqlAll(to.db, "SELECT call_id, state FROM questions", []),
    ).toEqual([{ call_id: "call_1", state: "open" }]);
  });

  test("a grant made before its challenge expired imports as granted, past its expiry", async () => {
    const { f, branch, events } = await replayCase(APPROVAL, 7, TS_LOG);
    const asked = (await events()).find((e) => e.type === "approval_requested");
    if (asked?.type !== "approval_requested") throw new Error("no challenge");
    const { challenge_id, call_id, args_hash } = asked.data;
    const writer = unwrap(await f.store.acquire(branch, "approver"));
    unwrap(
      await writer.append([
        {
          type: "approval_granted",
          type_version: 1,
          critical: true,
          actor: { kind: "approver", principal: alice },
          data: { challenge_id, call_id, args_hash },
        },
      ]),
    );
    await writer.release();
    const to = await imported(APPROVAL, f, branch);
    expect(await sqlAll(to.db, "SELECT state FROM approvals", [])).toEqual([
      { state: "granted" },
    ]);
  });

  test("an undecided challenge imports as open", async () => {
    const { f, branch } = await replayCase(APPROVAL, 7, TS_LOG);
    const to = await imported(APPROVAL, f, branch);
    expect(await sqlAll(to.db, "SELECT state FROM approvals", [])).toEqual([
      { state: "open" },
    ]);
  });
});
