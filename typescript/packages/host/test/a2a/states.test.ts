import { afterEach, expect, test } from "bun:test";
import { storeConnection } from "@threads/core/host";
import { z } from "zod";
import { DEFAULT_BUDGET } from "../../src";
import { alice, knownEventsOf } from "../kit";
import { sqlAll } from "../sql";
import { asker, reader, structured, talker, worker } from "./agents";
import { message, reaches, serve, stopAll, task } from "./kit";

// The rows of the design's state table that a run reaches on its own, driven end to end by a
// scripted model and asserted through GetTask, which is what a partner actually sees. The park rows
// live in parks.test.ts, which drives the approval flow they belong to.

afterEach(stopAll);

const ONE = { support: { description: "Support." } };

/** The run's own user_input, read back from the log the run really committed. */
async function inputOf(
  on: Awaited<ReturnType<typeof serve>>,
  taskId: string,
): Promise<
  Extract<
    Awaited<ReturnType<typeof knownEventsOf>>[number],
    { type: "user_input" }
  >
> {
  const { db } = await storeConnection(on.store);
  const branches = z
    .array(z.strictObject({ branch_id: z.string() }))
    .parse(await sqlAll(db, "SELECT DISTINCT branch_id FROM events", []));
  for (const { branch_id } of branches) {
    const found = (await knownEventsOf(on.store, alice.tenant, branch_id)).find(
      (e) => e.event_id === taskId,
    );
    if (found?.type === "user_input") return found;
  }
  throw new Error(`no user_input ${taskId} in the log`);
}

function textOf(t: Awaited<ReturnType<typeof task>>): string {
  return z
    .array(z.object({ text: z.string().optional() }))
    .parse(t.status.message?.parts ?? [])
    .map((p) => p.text ?? "")
    .join("");
}

async function send(
  on: Awaited<ReturnType<typeof serve>>,
  id = "m1",
  text = "hello",
): Promise<Awaited<ReturnType<typeof task>>> {
  return task(await on.rpc("SendMessage", message(id, text), { as: alice }));
}

test("an input recorded with no model request yet is SUBMITTED", async () => {
  const on = await serve({
    agents: { support: talker("hi") },
    a2a: { expose: ONE },
  });
  // The answer to SendMessage itself is read back before the run can have asked the model.
  const sent = await send(on);
  expect(sent.status.state).toBe("TASK_STATE_SUBMITTED");
  expect(sent.status.message).toBeUndefined();
});

test("a completed run is COMPLETED with one text artifact", async () => {
  const on = await serve({
    agents: { support: talker("the answer") },
    a2a: { expose: ONE },
  });
  const done = await reaches(on, alice, (await send(on)).id, [
    "TASK_STATE_COMPLETED",
  ]);
  expect(done.artifacts).toHaveLength(1);
  expect(done.artifacts?.[0]?.parts).toEqual([{ text: "the answer" }]);
  // A completed task carries no status message: there is nothing to explain.
  expect(done.status.message).toBeUndefined();
});

test("an agent with an output schema completes with a data part", async () => {
  const on = await serve({
    agents: { support: structured({ city: "Berlin" }) },
    a2a: { expose: ONE },
  });
  const done = await reaches(on, alice, (await send(on)).id, [
    "TASK_STATE_COMPLETED",
  ]);
  expect(done.artifacts?.[0]?.parts).toEqual([{ data: { city: "Berlin" } }]);
});

test("the artifact id is derived, so the same task always renders the same one", async () => {
  const on = await serve({
    agents: { support: talker("hi") },
    a2a: { expose: ONE },
  });
  const sent = await send(on);
  const first = await reaches(on, alice, sent.id, ["TASK_STATE_COMPLETED"]);
  const again = await reaches(on, alice, sent.id, ["TASK_STATE_COMPLETED"]);
  expect(again.artifacts?.[0]?.artifactId).toBe(
    first.artifacts?.[0]?.artifactId,
  );
});

test("an ask_user park is INPUT_REQUIRED with the question and its options", async () => {
  const on = await serve({
    agents: {
      support: asker({
        question: "Which colour?",
        options: ["red", "blue"],
        answer: "done",
      }),
    },
    a2a: { expose: ONE },
  });
  const asked = await reaches(on, alice, (await send(on)).id, [
    "TASK_STATE_INPUT_REQUIRED",
  ]);
  const text = textOf(asked);
  expect(text).toInclude("Which colour?");
  expect(text).toInclude("red");
  expect(text).toInclude("blue");
  // The status message is an agent message carrying the task's own ids.
  expect(asked.status.message?.role).toBe("ROLE_AGENT");
  expect(asked.status.message?.taskId).toBe(asked.id);
  expect(asked.status.message?.contextId).toBe(asked.contextId);
});

test("a free-text question is INPUT_REQUIRED with just the question", async () => {
  const on = await serve({
    agents: {
      support: asker({ question: "What is your order id?", answer: "done" }),
    },
    a2a: { expose: ONE },
  });
  const asked = await reaches(on, alice, (await send(on)).id, [
    "TASK_STATE_INPUT_REQUIRED",
  ]);
  expect(textOf(asked)).toBe("What is your order id?");
});

// The approval and effect parks are the rows in test/a2a/parks.test.ts, which drives the whole
// approval flow rather than just the state it shows.

test("the status message id is derived, so the same committed state renders the same message", async () => {
  const on = await serve({
    agents: {
      support: asker({ question: "Which colour?", answer: "done" }),
    },
    a2a: { expose: ONE },
  });
  const sent = await send(on);
  const first = await reaches(on, alice, sent.id, [
    "TASK_STATE_INPUT_REQUIRED",
  ]);
  const again = await reaches(on, alice, sent.id, [
    "TASK_STATE_INPUT_REQUIRED",
  ]);
  expect(again.status.message?.messageId).toBe(first.status.message?.messageId);
  expect(again.status.message?.messageId).toMatch(
    /^[0-9a-f]{8}-[0-9a-f]{4}-8[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
  );
});

test("a run that exhausts its budget after running is FAILED with budget_exhausted", async () => {
  const on = await serve({
    agents: { support: worker() },
    // One model request: the tool call is made, its second request is refused.
    a2a: {
      expose: {
        support: { description: "Support.", budget: { max_model_requests: 1 } },
      },
    },
  });
  const failed = await reaches(on, alice, (await send(on)).id, [
    "TASK_STATE_FAILED",
    "TASK_STATE_COMPLETED",
    "TASK_STATE_REJECTED",
  ]);
  // Exhausted after it ran, so FAILED and not REJECTED, and the code is in the status message.
  expect(failed.status.state).toBe("TASK_STATE_FAILED");
  expect(textOf(failed)).toStartWith("budget_exhausted: ");
  expect(textOf(failed)).toInclude("max_model_requests");
});

test("an exposed run carries the default budget when the config states none", async () => {
  const on = await serve({
    agents: { support: talker("hi") },
    a2a: { expose: ONE },
  });
  const sent = await send(on);
  // Read from the log, not from the option: this is the budget the run is really decided under.
  const input = (await inputOf(on, sent.id)).data;
  expect(input.budget).toEqual(DEFAULT_BUDGET);
});

test("an exposed run carries the configured budget when the config states one", async () => {
  const budget = { max_turns: 3, max_wall_ms: 5_000 };
  const on = await serve({
    agents: { support: talker("hi") },
    a2a: { expose: { support: { description: "Support.", budget } } },
  });
  const sent = await send(on);
  expect((await inputOf(on, sent.id)).data.budget).toEqual(budget);
});

test("a run refused before its first model request is REJECTED, not FAILED", async () => {
  const on = await serve({
    agents: { support: reader("hi") },
    // A budget too small for even one attempt: the run never gets to ask the model anything.
    a2a: {
      expose: {
        support: {
          description: "Support.",
          budget: { max_turns: 1, max_cost_nanos: 1 },
        },
      },
    },
  });
  const rejected = await reaches(on, alice, (await send(on)).id, [
    "TASK_STATE_REJECTED",
    "TASK_STATE_FAILED",
    "TASK_STATE_COMPLETED",
  ]);
  expect(rejected.status.state).toBe("TASK_STATE_REJECTED");
});

test("a task carries a timestamp from the log, not from the clock at read time", async () => {
  const on = await serve({
    agents: { support: talker("hi") },
    a2a: { expose: ONE },
  });
  const sent = await send(on);
  const done = await reaches(on, alice, sent.id, ["TASK_STATE_COMPLETED"]);
  const again = await reaches(on, alice, sent.id, ["TASK_STATE_COMPLETED"]);
  expect(again.status.timestamp).toBe(done.status.timestamp);
  expect(done.status.timestamp).toBeString();
});

test("one run at a time per context: a second message with no taskId is refused", async () => {
  const on = await serve({
    agents: {
      support: asker({ question: "Which colour?", answer: "done" }),
    },
    a2a: { expose: ONE },
  });
  const first = await task(
    await on.rpc("SendMessage", message("m1", "hi", { contextId: "c" }), {
      as: alice,
    }),
  );
  await reaches(on, alice, first.id, ["TASK_STATE_INPUT_REQUIRED"]);
  // A parked task is not "working", so this context can take a new run once it settles.
  const second = await on.rpc(
    "SendMessage",
    message("m2", "hi again", { contextId: "c" }),
    { as: alice },
  );
  expect(second.status).toBe(200);
});
