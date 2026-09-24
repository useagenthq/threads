import { describe, expect, test } from "bun:test";
import { z } from "zod";
import { agent, scriptedModel, sqlite } from "../../src";
import type { KnownEvent } from "../../src/log";
import { assertTeamReplays } from "./kit";
import {
  answering,
  call,
  events,
  logOf,
  memberEvents,
  receipts,
  replyTo,
  say,
  start,
  types,
} from "./run-kit";

// ask and reply in a running team (spec/schema/README.md, "Teams"; design §4.8 and §4.9): the
// asker's call stays pending and its turn parks; the asked member's reply is control mail that
// closes the ask answered, resumes the asker and records the call's one result, and the turn
// goes on. Every test ends with the team replaying from its logs.

/** A model whose `n`th answer (from 0) is made from its rendered request. */
function scripted(answers: readonly ((request: string) => unknown)[]) {
  let n = 0;
  return answering((request) => {
    const next = answers[n] ?? (() => say("Nothing more."));
    n += 1;
    return next(request);
  });
}

const Preview = z.object({ status: z.string() }).loose();

/** The value a call's one tool_result records. */
function resultOf(log: readonly KnownEvent[], callId: string): unknown {
  const results = log.filter(
    (e) => e.type === "tool_result" && e.data.call_id === callId,
  );
  expect(results).toHaveLength(1);
  const [only] = results;
  return only?.type === "tool_result"
    ? Preview.parse(JSON.parse(only.data.preview))
    : undefined;
}

describe("ask and reply", () => {
  test("the lead asks a member: its reply is the ask's result, and the turn goes on", async () => {
    const store = sqlite(":memory:");
    const researcher = agent({
      name: "researcher",
      model: scripted([
        () => say("Read about batteries."),
        (request) => replyTo("r1", request, "Batteries."),
        () => say("Replied."),
      ]),
    });
    const lead = agent({
      name: "lead",
      model: scriptedModel({
        responses: [
          start("c1", "researcher", "Topic: batteries."),
          call("c2", "ask", { to: "researcher-1", question: "Which topic?" }),
          say("It read about batteries."),
          say("Final."),
          say("Final."),
        ],
      }),
      team: [researcher],
    });
    const r = await lead.run("Find the topic.", { store });
    expect(r.status === "completed" && r.output).toBe("Final.");
    const log = await events(store, r.thread);
    expect(resultOf(log, "c2")).toMatchObject({
      status: "answered",
      text: "Batteries.",
      member: { name: "researcher-1", generation: 1 },
    });
    // Parked on the ask, resumed by the reply: the reply's receipt, the close, the resume and the
    // call's result are one append.
    const closed = types(log).indexOf("ask_closed");
    expect(types(log).slice(closed - 1, closed + 3)).toEqual([
      "message_received",
      "ask_closed",
      "resumed",
      "tool_result",
    ]);
    expect(types(log)).toContain("parked");
    const member = await memberEvents(store, r.team.ref.id, "researcher-1");
    expect(receipts(member, "ask")).toHaveLength(1);
    assertTeamReplays(await logOf(store), r.team.ref.id);
  });

  test("a member asks another member and parks; the reply runs it on and it settles", async () => {
    const store = sqlite(":memory:");
    const researcher = agent({
      name: "researcher",
      model: scripted([
        () => say("Read."),
        (request) => replyTo("r1", request, "Batteries."),
        () => say("Replied."),
      ]),
    });
    const writer = agent({
      name: "writer",
      model: scriptedModel({
        responses: [
          call("w1", "ask", { to: "researcher-1", question: "Which topic?" }),
          say("Report: batteries."),
        ],
      }),
    });
    const lead = agent({
      name: "lead",
      model: scriptedModel({
        responses: [
          start("c1", "researcher", "Read the notes."),
          start("c2", "writer", "Ask researcher-1 its topic, then report."),
          say("Started."),
          say("Final."),
          say("Final."),
          say("Final."),
        ],
      }),
      team: [researcher, writer],
    });
    const r = await lead.run("Report.", { store });
    expect(r.status === "completed" && r.output).toBe("Final.");
    const member = await memberEvents(store, r.team.ref.id, "writer-1");
    expect(resultOf(member, "w1")).toMatchObject({
      status: "answered",
      text: "Batteries.",
    });
    expect(types(member)).toContain("member_idle");
    // The writer's first park sent its lead one member_parked notice.
    expect(
      receipts(await events(store, r.thread), "member_parked"),
    ).toHaveLength(1);
    assertTeamReplays(await logOf(store), r.team.ref.id);
  });

  test("an ask of an unknown member is refused as the call's result, and nothing parks", async () => {
    const store = sqlite(":memory:");
    const lead = agent({
      name: "lead",
      model: scriptedModel({
        responses: [
          call("c1", "ask", { to: "nobody-1", question: "Hello?" }),
          say("Nobody is there."),
        ],
      }),
      team: [
        agent({ name: "researcher", model: scriptedModel({ responses: [] }) }),
      ],
    });
    const r = await lead.run("Ask.", { store });
    expect(r.status === "completed" && r.output).toBe("Nobody is there.");
    const log = await events(store, r.thread);
    expect(resultOf(log, "c1")).toEqual({
      code: "unknown_member",
      status: "refused",
    });
    expect(types(log)).not.toContain("parked");
    assertTeamReplays(await logOf(store), r.team.ref.id);
  });
});
