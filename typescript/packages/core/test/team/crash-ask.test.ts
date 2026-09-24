import { describe, expect, test } from "bun:test";
import { z } from "zod";
import { agent, scriptedModel } from "../../src";
import type { KnownEvent } from "../../src/log";
import { BranchId } from "../../src/log";
import { knownEvents } from "../../src/reduce";
import { memberRows } from "../../src/team/rows";
import { unwrap } from "../store/helpers";
import { Crash, crashing, drill, type Point } from "./crash-kit";
import { assertTeamReplays } from "./kit";
import {
  answering,
  askIds,
  call,
  replyTo,
  resultOf,
  say,
  start,
} from "./run-kit";

// Crash drills at the commit points of an ask and a wait (design §7, Phase 1 proofs): the process
// dies inside the transaction that opens the ask, sends the reply, closes the ask, registers the
// wait, fires the member's settle notice, or finishes the wait. Nothing of that transaction is
// stored; the host's recovery of the open run finishes it with exactly one of each.

/** A researcher that replies to an ask it hasn't answered yet, and otherwise reports. */
const researcher = () =>
  agent({
    name: "researcher",
    model: answering((request) =>
      askIds(request).length > 0 && !request.includes('\\"name\\":\\"reply\\"')
        ? replyTo("r1", request, "Batteries.")
        : say("Done."),
    ),
  });

const lead = (first: readonly unknown[]) =>
  agent({
    name: "lead",
    model:
      first.length === 0
        ? answering(() => say("Final."))
        : scriptedModel({
            responses: [
              ...first,
              ...Array.from({ length: 4 }, () => say("Final.")),
            ],
          }),
    team: [researcher()],
  });

const ASK: readonly unknown[] = [
  start("c1", "researcher", "Read."),
  call("c2", "ask", { to: "researcher-1", question: "Which topic?" }),
];
const WAIT: readonly unknown[] = [
  start("c1", "researcher", "Read."),
  call("c2", "wait", { members: ["researcher-1"] }),
];

const ASK_POINTS: readonly Point[] = [
  { name: "the lead's ask", at: (sql) => sql.includes("INSERT INTO asks") },
  {
    name: "the member's reply",
    at: (sql, params) =>
      sql.includes("INSERT INTO mail") && params[2] === "reply",
  },
  {
    name: "the lead's close of the ask",
    at: (sql) => sql.includes("UPDATE asks SET state"),
  },
];
const WAIT_POINTS: readonly Point[] = [
  {
    name: "the lead's wait",
    at: (sql, params) =>
      sql.includes("INSERT INTO monitors") && params[5] === "settle",
  },
  {
    name: "the member's settle notice",
    at: (sql, params) =>
      sql.includes("DELETE FROM monitors WHERE monitor_id") &&
      params.some((p) => typeof p === "string" && p.endsWith(":researcher-1")),
  },
  {
    name: "the wait's finish",
    at: (sql) => sql.includes("DELETE FROM monitors WHERE wait_id"),
  },
];

const count = (log: readonly KnownEvent[], type: string): number =>
  log.filter((e) => e.type === type).length;

async function crashThenRestart(point: Point, first: readonly unknown[]) {
  const d = drill();
  await expect(
    lead(first).run("Work.", { store: d.open(crashing(d.db, point)) }),
  ).rejects.toThrow(Crash);
  const { result, branch, team } = await d.restart(lead([]));
  expect(result.status).toBe("completed");
  const leadLog = knownEvents(unwrap(d.log.read(branch)));
  const row = memberRows(d.db, team).find((r) => r.name === "researcher-1");
  const member = knownEvents(
    unwrap(d.log.read(BranchId.parse(z.string().parse(row?.branch_id)))),
  );
  return { d, team, leadLog, member };
}

describe("ask crash drills", () => {
  for (const point of ASK_POINTS)
    test(`a crash inside ${point.name} stores none of it; the restart closes the ask once`, async () => {
      const { d, team, leadLog, member } = await crashThenRestart(point, ASK);
      const asks = leadLog.filter(
        (e) => e.type === "message_sent" && e.data.envelope.kind === "ask",
      );
      expect(asks).toHaveLength(1);
      expect(count(leadLog, "ask_closed")).toBe(1);
      expect(resultOf(leadLog, "c2")).toMatchObject({
        status: "answered",
        text: "Batteries.",
      });
      const replies = member.filter(
        (e) => e.type === "message_sent" && e.data.envelope.kind === "reply",
      );
      expect(replies).toHaveLength(1);
      assertTeamReplays(d.log, team);
    });
});

describe("wait crash drills", () => {
  for (const point of WAIT_POINTS)
    test(`a crash inside ${point.name} stores none of it; the restart finishes the wait once`, async () => {
      const { d, team, leadLog } = await crashThenRestart(point, WAIT);
      expect(count(leadLog, "wait_started")).toBe(1);
      expect(count(leadLog, "wait_finished")).toBe(1);
      // A crash between the member's answer and its turn's end ends that turn interrupted on
      // recovery (API run recovery), so the member settles failed there; either way it settles
      // once and the wait counts it.
      expect(resultOf(leadLog, "c2")).toMatchObject({
        status: "waited",
        timed_out: false,
        finished: [{ member: { name: "researcher-1" } }],
      });
      assertTeamReplays(d.log, team);
    });
});
