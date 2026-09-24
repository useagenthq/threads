import { describe, expect, test } from "bun:test";
import { agent, scriptedModel, sqlite } from "../../src";
import { teamRow } from "../../src/team/rows";
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
  usage,
} from "./run-kit";

// A lead's run with its team, in-process (spec/schema/README.md, "Teams" and "Run completion"):
// it starts members, answers, wakes for each run-owned member's settlement and returns the last
// answer with the team. Every test ends with the team's replay check.

const researcher = (answer: string) =>
  agent({
    name: "researcher",
    model: scriptedModel({ responses: [say(answer)] }),
  });

describe("a team lead's run", () => {
  test("first answer, the member settles, the lead wakes and gives the final answer", async () => {
    const store = sqlite(":memory:");
    const lead = agent({
      name: "lead",
      model: scriptedModel({
        responses: [
          start("c1", "researcher", "Batteries."),
          say("Started the researcher."),
          say("The researcher says battery prices fell."),
        ],
      }),
      team: [researcher("Battery prices fell.")],
    });
    const r = await lead.run("Research batteries.", { store });
    expect(r.status === "completed" && r.output).toBe(
      "The researcher says battery prices fell.",
    );
    const log = await logOf(store);
    expect(r.team.ref).toEqual({ tenant: log.tenant, id: r.team.ref.id });
    const lead0 = await events(store, r.thread);
    // Both answers stay in the timeline; each lead turn that ended well left it idle.
    expect(types(lead0).filter((t) => t === "member_idle")).toHaveLength(2);
    expect(receipts(lead0, "member_settled")).toHaveLength(1);
    const member = await memberEvents(store, r.team.ref.id, "researcher-1");
    expect(types(member)).toEqual([
      "thread_started",
      "user_input",
      "model_request",
      "model_response",
      "turn_completed",
      "member_idle",
      "message_sent",
    ]);
    assertTeamReplays(log, r.team.ref.id);
  });

  test("a member's send to the lead opens a lead turn of the same run", async () => {
    const store = sqlite(":memory:");
    const chatty = agent({
      name: "researcher",
      model: scriptedModel({
        responses: [
          call("m1", "send", { to: "lead", text: "Halfway there." }),
          say("Done."),
        ],
      }),
    });
    const lead = agent({
      name: "lead",
      model: scriptedModel({
        responses: [
          start("c1", "researcher", "Go."),
          say("Started."),
          say("Noted."),
          say("All done."),
        ],
      }),
      team: [chatty],
    });
    const r = await lead.run("Work.", { store });
    expect(r.status === "completed" && r.output).toBe("All done.");
    const log = await events(store, r.thread);
    expect(receipts(log, "message")).toHaveLength(1);
    expect(receipts(log, "member_settled")).toHaveLength(1);
    assertTeamReplays(await logOf(store), r.team.ref.id);
  });

  test("a wake turn that fails after the first answer fails the run and closes the team", async () => {
    const store = sqlite(":memory:");
    // The lead's script ends after its first answer: its wake turn's request is refused.
    const lead = agent({
      name: "lead",
      model: scriptedModel({
        responses: [start("c1", "researcher", "Go."), say("Started.")],
      }),
      team: [researcher("Done.")],
    });
    const r = await lead.run("Work.", { store });
    expect(r.status).toBe("failed");
    const log = await logOf(store);
    const lead0 = await events(store, r.thread);
    // The lead's end closes its team and cancels its live members, in the same append.
    expect(types(lead0).slice(-3)).toEqual([
      "turn_completed",
      "member_ended",
      "message_sent",
    ]);
    expect(teamRow(log.driver, r.team.ref.id)?.closed_at).not.toBeNull();
    assertTeamReplays(log, r.team.ref.id);
  });

  test("team: [] is a team no model can grow: start is refused unknown_agent", async () => {
    const store = sqlite(":memory:");
    const lead = agent({
      name: "lead",
      model: scriptedModel({
        responses: [start("c1", "researcher", "Go."), say("No one to start.")],
      }),
      team: [],
    });
    const r = await lead.run("Work.", { store });
    expect(r.status === "completed" && r.output).toBe("No one to start.");
    const log = await events(store, r.thread);
    const result = log.find((e) => e.type === "tool_result");
    expect(result?.type === "tool_result" && result.data.preview).toBe(
      '{"code":"unknown_agent","status":"refused"}',
    );
    expect(types(log)).toContain("message_policy_decided");
    assertTeamReplays(await logOf(store), r.team.ref.id);
  });

  test("teamLimits.concurrent caps the members starting and running at once", async () => {
    const store = sqlite(":memory:");
    const both = {
      content: [
        {
          type: "tool_use",
          call_id: "c1",
          name: "start",
          input: { agent: "researcher", task: "One." },
        },
        {
          type: "tool_use",
          call_id: "c2",
          name: "start",
          input: { agent: "researcher", task: "Two." },
        },
      ],
      stop_reason: "tool_use",
      usage,
    };
    const lead = agent({
      name: "lead",
      model: scriptedModel({
        responses: [both, say("One started."), say("It reported.")],
      }),
      team: [researcher("Done.")],
      teamLimits: { concurrent: 1 },
    });
    const r = await lead.run("Work.", { store });
    expect(r.status === "completed" && r.output).toBe("It reported.");
    const previews = (await events(store, r.thread)).flatMap((e) =>
      e.type === "tool_result" ? [e.data.preview] : [],
    );
    expect(previews[1]).toBe('{"code":"concurrency_cap","status":"refused"}');
    assertTeamReplays(await logOf(store), r.team.ref.id);
  });
});
