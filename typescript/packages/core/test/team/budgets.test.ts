import { describe, expect, test } from "bun:test";
import { z } from "zod";
import { agent, type Model, scriptedModel, sqlite } from "../../src";
import type { Principal } from "../../src/log";
import { markTestKit } from "../../src/model/guard";
import { assertTeamReplays, query } from "./kit";
import {
  call,
  events,
  logOf,
  memberEvents,
  say,
  start,
  types,
} from "./run-kit";

// A team's budgets (spec/schema/README.md, "Teams"; design §2.6): a member's model requests are
// reserved against its own budget, every ancestor thread's budget, and the run budget of the
// request its turn belongs to; start checks headroom first.

const Row = z.array(
  z.object({ budget_id: z.string(), attempt_key: z.string() }),
);

/** Which budgets each member attempt was reserved against. */
async function reservedFor(
  store: ReturnType<typeof sqlite>,
  branch: string,
): Promise<readonly string[]> {
  const log = await logOf(store);
  return Row.parse(
    await query(
      log.driver,
      "SELECT budget_id, attempt_key FROM budget_ledger WHERE limit_name = 'max_model_requests' ORDER BY attempt_key, budget_id",
      [],
    ),
  )
    .filter((r) => r.attempt_key.startsWith(`${branch}:`))
    .map((r) => r.budget_id);
}

/** A scripted model whose `nth` request (from 1) waits for `open`. */
function waitsOn(
  nth: number,
  responses: readonly unknown[],
  open: Promise<void>,
): Model {
  const model = scriptedModel({ responses: [...responses] });
  let n = 0;
  const made: Model = {
    ...model,
    send: async function* (...args: Parameters<Model["send"]>) {
      n += 1;
      if (n === nth) await open;
      yield* model.send(...args);
    },
  };
  markTestKit(made);
  return made;
}

/** A scripted model that reports each request it is sent. */
function reporting(responses: readonly unknown[], sent: () => void): Model {
  const model = scriptedModel({ responses: [...responses] });
  const made: Model = {
    ...model,
    send: async function* (...args: Parameters<Model["send"]>) {
      sent();
      yield* model.send(...args);
    },
  };
  markTestKit(made);
  return made;
}

describe("team budgets", () => {
  test("a member exhausts its request's run budget: it ends budget_exhausted and so does the run", async () => {
    const store = sqlite(":memory:");
    const lead = agent({
      name: "lead",
      model: scriptedModel({
        responses: [start("c1", "researcher", "Go."), say("Started.")],
      }),
      team: [
        agent({
          name: "researcher",
          model: scriptedModel({ responses: [say("never")] }),
        }),
      ],
    });
    const r = await lead.run("Work.", {
      store,
      budget: { max_model_requests: 2 },
    });
    expect(r.status).toBe("budget_exhausted");
    const member = await memberEvents(store, r.team.ref.id, "researcher-1");
    expect(types(member)).not.toContain("model_request");
    const ended = member.find((e) => e.type === "member_ended");
    expect(ended?.type === "member_ended" && ended.data.result).toMatchObject({
      status: "budget_exhausted",
      budget: { scope: "run", limit: "max_model_requests", limit_value: 2 },
    });
    await assertTeamReplays(await logOf(store), r.team.ref.id);
  });

  test("start without headroom for one request of the member's model is refused budget_exceeded", async () => {
    const store = sqlite(":memory:");
    const lead = agent({
      name: "lead",
      model: scriptedModel({ responses: [start("c1", "researcher", "Go.")] }),
      team: [
        agent({
          name: "researcher",
          model: scriptedModel({ responses: [] }),
        }),
      ],
    });
    const r = await lead.run("Work.", {
      store,
      budget: { max_model_requests: 1 },
    });
    expect(r.status).toBe("budget_exhausted");
    const result = (await events(store, r.thread)).find(
      (e) => e.type === "tool_result",
    );
    expect(result?.type === "tool_result" && result.data.preview).toBe(
      '{"code":"budget_exceeded","status":"refused"}',
    );
    await assertTeamReplays(await logOf(store), r.team.ref.id);
  });

  test("a member's requests reserve against the lead's thread budget, and each turn against its own request's run budget", async () => {
    const store = sqlite(":memory:");
    const bob: Principal = { issuer: "api", tenant: "local", subject: "bob" };
    const researched = Promise.withResolvers<void>();
    let requests = 0;
    const researcher = agent({
      name: "researcher",
      model: reporting([say("Task done."), say("Message done.")], () => {
        requests += 1;
        if (requests === 2) researched.resolve();
      }),
    });
    const lead = agent({
      name: "lead",
      budget: { max_model_requests: 50 },
      // Its last answer waits until the researcher has taken the second run's message.
      model: waitsOn(
        5,
        [
          start("c1", "researcher", "Go."),
          say("Started."),
          say("Reported."),
          call("c2", "send", { to: "researcher-1", text: "One more." }),
          say("Sent."),
        ],
        researched.promise,
      ),
      team: [researcher],
    });
    const first = await lead.run("Work.", {
      store,
      budget: { max_model_requests: 20 },
    });
    const second = await lead.run("And more.", {
      store,
      thread: first.thread,
      principal: bob,
      budget: { max_model_requests: 30 },
    });
    expect(second.status === "completed" && second.output).toBe("Sent.");
    const inputs = (await events(store, first.thread)).flatMap((e) =>
      e.type === "user_input" ? [e.event_id] : [],
    );
    const member = await memberEvents(store, first.team.ref.id, "researcher-1");
    const branch = member[0]?.branch_id ?? "";
    const lead0 = first.thread.id;
    expect(await reservedFor(store, branch)).toEqual([
      `run:${lead0}:${inputs[0]}`,
      `thread:${lead0}`,
      `run:${lead0}:${inputs[1]}`,
      `thread:${lead0}`,
    ]);
    await assertTeamReplays(await logOf(store), first.team.ref.id);
  });
});
