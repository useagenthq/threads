import { afterEach, describe, expect, test } from "bun:test";
import { agent, scriptedModel } from "@threads/core";
import { z } from "zod";
import {
  alice,
  eventsOf,
  type Harness,
  harness,
  say,
  sseMessages,
  use,
} from "./kit";

// A team lead behind the host (spec/schema/README.md, "Teams"): a run started over the HTTP API
// opens the lead's team in its first append, the lead starts a member, and the run's result is
// the lead's answer after the member reported.

let h: Harness | undefined;
afterEach(async () => {
  await h?.host.stop();
  h = undefined;
});

const Accepted = z.object({ thread_id: z.string(), run_id: z.string() });
const Result = z.object({
  result: z.object({ status: z.string(), output: z.string().optional() }),
});

describe("a team lead behind the host", () => {
  test("a run over the API opens the team, starts a member and answers after it reports", async () => {
    const researcher = agent({
      name: "researcher",
      model: scriptedModel({ responses: [say("Prices fell.")] }),
    });
    const lead = agent({
      name: "lead",
      model: scriptedModel({
        responses: [
          use("start", { agent: "researcher", task: "Go." }, "c1"),
          say("Started."),
          say("The researcher says prices fell."),
        ],
      }),
      team: [researcher],
    });
    h = harness({ agents: { lead } });
    const accepted = Accepted.parse(
      await (
        await h.call("POST", "/v1/runs", {
          as: alice,
          body: { agent: "lead", input: "Research prices." },
          headers: { "idempotency-key": "k-1" },
        })
      ).json(),
    );
    const last = (
      await sseMessages(
        await h.call(
          "GET",
          `/v1/threads/${accepted.thread_id}/runs/${accepted.run_id}/events`,
          { as: alice },
        ),
      )
    ).at(-1);
    expect(Result.parse(last).result).toEqual({
      status: "completed",
      output: "The researcher says prices fell.",
    });
    const branches = z.array(z.object({ branch_id: z.string() })).parse(
      await (
        await h.call("GET", `/v1/threads/${accepted.thread_id}/branches`, {
          as: alice,
        })
      ).json(),
    );
    const events = await eventsOf(
      h.store,
      alice.tenant,
      branches[0]?.branch_id ?? "",
    );
    const started = events.find((e) => e.type === "thread_started");
    expect(
      z
        .object({ data: z.object({ team: z.object({ id: z.string() }) }) })
        .safeParse(started).success,
    ).toBe(true);
    expect(events.map((e) => e.type)).toContain("member_started");
  });
});
