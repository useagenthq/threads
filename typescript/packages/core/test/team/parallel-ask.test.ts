import { describe, expect, test } from "bun:test";
import { agent, scriptedModel, sqlite } from "../../src";
import { assertTeamReplays } from "./kit";
import {
  answers,
  events,
  logOf,
  replyTo,
  resultOf,
  say,
  start,
  types,
  usage,
} from "./run-kit";

// Team calls beside other calls in one response (design §4.8 and §4.12): an opened ask or wait
// parks its turn only once nothing else in the turn is runnable, with one park per ask or wait.
// The other calls run first, and each answer resumes its own park. Reviewer probes H1 (21E.1).

/** One response making several tool calls. */
const calls = (...made: readonly [string, string, unknown][]): unknown => ({
  content: made.map(([id, name, input]) => ({
    type: "tool_use",
    call_id: id,
    name,
    input,
  })),
  stop_reason: "tool_use",
  usage,
});

/** A member that reads its task, then replies to the ask it is shown. */
const replier = (name: string) =>
  agent({
    name,
    model: answers([
      () => say("Read."),
      (request) => replyTo("r1", request, `${name} answer.`),
      () => say("Replied."),
    ]),
  });

async function runLead(
  response: unknown,
  team: NonNullable<Parameters<typeof agent>[0]["team"]>,
) {
  const store = sqlite(":memory:");
  const lead = agent({
    name: "lead",
    model: scriptedModel({
      responses: [
        start("c1", "a", "Go."),
        start("c2", "b", "Go."),
        response,
        ...Array.from({ length: 6 }, () => say("Final.")),
      ],
    }),
    team,
  });
  const r = await lead.run("Go.", { store });
  expect(r.status).toBe("completed");
  const log = await events(store, r.thread);
  await assertTeamReplays(await logOf(store), r.team.ref.id);
  return log;
}

describe("an ask or a wait beside other calls", () => {
  test("two asks in one response: both park, and each reply closes its own", async () => {
    const log = await runLead(
      calls(
        ["q1", "ask", { to: "a-1", question: "A?" }],
        ["q2", "ask", { to: "b-1", question: "B?" }],
      ),
      [replier("a"), replier("b")],
    );
    expect(resultOf(log, "q1")).toMatchObject({
      status: "answered",
      text: "a answer.",
    });
    expect(resultOf(log, "q2")).toMatchObject({
      status: "answered",
      text: "b answer.",
    });
    expect(types(log).filter((t) => t === "parked")).toHaveLength(2);
    expect(types(log).filter((t) => t === "resumed")).toHaveLength(2);
  }, 15_000);

  test("an ask and a wait in one response: both close", async () => {
    const log = await runLead(
      calls(
        ["q1", "ask", { to: "a-1", question: "A?" }],
        ["q2", "wait", { members: ["b-1"] }],
      ),
      [
        replier("a"),
        agent({
          name: "b",
          model: scriptedModel({ responses: [say("b done.")] }),
        }),
      ],
    );
    expect(resultOf(log, "q1")).toMatchObject({ status: "answered" });
    expect(resultOf(log, "q2")).toMatchObject({
      status: "waited",
      timed_out: false,
    });
  }, 15_000);

  test("an ask then a send: the send runs, then the ask parks and closes", async () => {
    const log = await runLead(
      calls(
        ["q1", "ask", { to: "a-1", question: "A?" }],
        ["q2", "send", { to: "b-1", text: "FYI." }],
      ),
      [
        replier("a"),
        agent({
          name: "b",
          model: scriptedModel({ responses: [say("b done."), say("Noted.")] }),
        }),
      ],
    );
    expect(resultOf(log, "q1")).toMatchObject({ status: "answered" });
    expect(resultOf(log, "q2")).toMatchObject({ status: "sent" });
    const at = (id: string) =>
      log.findIndex((e) => e.type === "tool_result" && e.data.call_id === id);
    expect(at("q2")).toBeLessThan(at("q1"));
    expect(types(log).filter((t) => t === "parked")).toHaveLength(1);
  }, 15_000);
});
