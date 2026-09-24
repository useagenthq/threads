import { describe, expect, test } from "bun:test";
import { z } from "zod";
import { agent, openThread, scriptedModel, sqlite, tool } from "../../src";
import { hostRunner } from "../../src/agent/registry";
import { memberRows } from "../../src/team/rows";
import { unwrap } from "../store/helpers";
import { assertTeamReplays } from "./kit";
import {
  call,
  events,
  logOf,
  memberEvents,
  receipts,
  say,
  start,
  types,
} from "./run-kit";

// A member that parks (design §2.7.1): its first park sends its starter one member_parked
// notice; the lead parks on the member and run() returns parked. Once what the member waits on is
// answered, running the lead again runs the member on, and its settlement resumes the lead.

const operator = { issuer: "api", tenant: "local", subject: "operator" };

/** A lead whose researcher's email needs an approval; its run has returned parked. */
async function parkedTeam(leadAnswers: readonly string[]) {
  const store = sqlite(":memory:");
  const sent: string[] = [];
  const mail = tool({
    name: "send_email",
    description: "Send an email.",
    input: z.object({ to: z.string() }),
    runs: "host",
    execute: async ({ to }) => {
      sent.push(to);
      return "sent";
    },
  });
  const researcher = agent({
    name: "researcher",
    model: scriptedModel({
      responses: [call("m1", "send_email", { to: "bob" }), say("Mailed bob.")],
    }),
    tools: [mail],
  });
  const lead = agent({
    name: "lead",
    model: scriptedModel({
      responses: [
        start("c1", "researcher", "Mail bob."),
        say("Started."),
        ...leadAnswers.map((a) => say(a)),
      ],
    }),
    team: [researcher],
  });
  const r = await lead.run("Get bob mailed.", { store });
  const approve = async (): Promise<void> => {
    const log = await logOf(store);
    const row = memberRows(log.driver, r.team.ref.id).find(
      (m) => m.name === "researcher-1",
    );
    if (row === undefined) throw new Error("the member has a row");
    const handle = unwrap(await openThread(store, row.thread_id));
    const [pending] = await handle.pendingApprovals();
    if (pending === undefined) throw new Error("the member waits on approval");
    unwrap(await handle.approve(pending.challenge_id, operator));
  };
  return { store, sent, lead, r, approve };
}

describe("a member that parks", () => {
  test("parks its lead: run() returns parked on the member, with one notice", async () => {
    const { store, r } = await parkedTeam([]);
    expect(r).toMatchObject({ status: "parked", reason: "awaiting_member" });
    expect(
      r.status === "parked" &&
        r.pending[0]?.id.startsWith(`${r.thread.branch}:`),
    ).toBe(true);
    const member = await memberEvents(store, r.team.ref.id, "researcher-1");
    // One notice for the member's first park, in the park's own append.
    const parked = types(member).indexOf("parked");
    expect(types(member)[parked + 1]).toBe("message_sent");
    expect(
      receipts(await events(store, r.thread), "member_parked"),
    ).toHaveLength(1);
    assertTeamReplays(await logOf(store), r.team.ref.id);
  });

  test("after the member's approval, the host's recovery runs the member on and resumes the lead", async () => {
    const { store, sent, lead, r, approve } = await parkedTeam([
      "The researcher mailed bob.",
    ]);
    await approve();
    const runner = hostRunner(lead);
    if (runner === undefined) throw new Error("agent() registers a runner");
    const resumed = await runner.execute(
      { store, principal: operator, thread: r.thread },
      [],
    );
    expect(resumed).toMatchObject({
      status: "completed",
      output: "The researcher mailed bob.",
    });
    expect(sent).toEqual(["bob"]);
    const lead0 = await events(store, r.thread);
    expect(receipts(lead0, "member_settled")).toHaveLength(1);
    expect(types(lead0)).toContain("resumed");
    assertTeamReplays(await logOf(store), r.team.ref.id);
  });

  test("after the member's approval, a new input first resumes the lead, then starts its turn", async () => {
    const { store, sent, lead, r, approve } = await parkedTeam([
      "The researcher mailed bob.",
      "Nothing else.",
    ]);
    await approve();
    const next = await lead.run("Anything else?", { store, thread: r.thread });
    expect(next).toMatchObject({
      status: "completed",
      output: "Nothing else.",
    });
    expect(sent).toEqual(["bob"]);
    const inputs = (await events(store, r.thread)).filter(
      (e) => e.type === "user_input",
    );
    expect(inputs).toHaveLength(2);
    assertTeamReplays(await logOf(store), r.team.ref.id);
  });
});
