import { afterEach, expect, test } from "bun:test";
import { alice, bob } from "../kit";
import { asker, talker } from "./agents";
import { faultName, message, reaches, serve, stopAll, task } from "./kit";

// Continuing a task: a message with a taskId answers the run's open ask_user, and only while the
// task is INPUT_REQUIRED. A retried answer returns the task rather than an error, because a caller
// that is in doubt about us must never be punished for asking again.

afterEach(stopAll);

const ONE = { support: { description: "Support." } };

const colours = () =>
  serve({
    agents: {
      support: asker({
        question: "Which colour?",
        options: ["red", "blue"],
        answer: "painted it red",
      }),
    },
    a2a: { expose: ONE },
  });

async function asked(
  on: Awaited<ReturnType<typeof serve>>,
  id = "m1",
): Promise<Awaited<ReturnType<typeof task>>> {
  const sent = await task(
    await on.rpc("SendMessage", message(id, "paint it"), { as: alice }),
  );
  return reaches(on, alice, sent.id, ["TASK_STATE_INPUT_REQUIRED"]);
}

function answer(
  messageId: string,
  text: string,
  taskId: string,
  contextId: string,
): unknown {
  return message(messageId, text, { taskId, contextId });
}

test("an answer that is one of the options continues the run", async () => {
  const on = await colours();
  const open = await asked(on);
  const continued = await on.rpc(
    "SendMessage",
    answer("a1", "red", open.id, open.contextId ?? ""),
    { as: alice },
  );
  expect(continued.status).toBe(200);
  const done = await reaches(on, alice, open.id, ["TASK_STATE_COMPLETED"]);
  expect(done.artifacts?.[0]?.parts).toEqual([{ text: "painted it red" }]);
});

test("an answer that is not one of the options is InvalidParamsError listing them", async () => {
  const on = await colours();
  const open = await asked(on);
  const refused = await on.rpc(
    "SendMessage",
    answer("a1", "green", open.id, open.contextId ?? ""),
    { as: alice },
  );
  expect(await faultName(refused.clone())).toBe("InvalidParamsError");
  const body = await refused.json();
  expect(JSON.stringify(body)).toInclude("red");
  expect(JSON.stringify(body)).toInclude("blue");
  // The question is still open, so the caller can answer it properly.
  expect(
    (await reaches(on, alice, open.id, ["TASK_STATE_INPUT_REQUIRED"])).id,
  ).toBe(open.id);
});

test("a retried continuation returns the task, not an error", async () => {
  const on = await colours();
  const open = await asked(on);
  const body = answer("a1", "red", open.id, open.contextId ?? "");
  const first = await task(await on.rpc("SendMessage", body, { as: alice }));
  const again = await task(await on.rpc("SendMessage", body, { as: alice }));
  expect(again.id).toBe(first.id);
  expect(again.contextId).toBe(first.contextId);
  // And the answer was recorded once: the run completes with the single answer it was given.
  const done = await reaches(on, alice, open.id, ["TASK_STATE_COMPLETED"]);
  expect(done.artifacts?.[0]?.parts).toEqual([{ text: "painted it red" }]);
});

test("a continuation reusing a messageId with another body is InvalidParamsError", async () => {
  const on = await colours();
  const open = await asked(on);
  await task(
    await on.rpc(
      "SendMessage",
      answer("a1", "red", open.id, open.contextId ?? ""),
      { as: alice },
    ),
  );
  const other = await on.rpc(
    "SendMessage",
    answer("a1", "blue", open.id, open.contextId ?? ""),
    { as: alice },
  );
  expect(await faultName(other)).toBe("InvalidParamsError");
});

test("continuing a terminal task is UnsupportedOperationError", async () => {
  const on = await serve({
    agents: { support: talker("all done") },
    a2a: { expose: ONE },
  });
  const sent = await task(
    await on.rpc("SendMessage", message("m1", "hi"), { as: alice }),
  );
  const done = await reaches(on, alice, sent.id, ["TASK_STATE_COMPLETED"]);
  const after = await on.rpc(
    "SendMessage",
    answer("a1", "more", done.id, done.contextId ?? ""),
    { as: alice },
  );
  expect(await faultName(after)).toBe("UnsupportedOperationError");
});

test("continuing a task that is still working is UnsupportedOperationError", async () => {
  const on = await colours();
  // The answer to SendMessage is read back before the run can have parked, so the task is not
  // INPUT_REQUIRED yet and a continuation has nothing to answer.
  const sent = await task(
    await on.rpc("SendMessage", message("m1", "paint it"), { as: alice }),
  );
  const early = await on.rpc(
    "SendMessage",
    answer("a1", "red", sent.id, sent.contextId ?? ""),
    { as: alice },
  );
  expect(await faultName(early)).toBe("UnsupportedOperationError");
  await reaches(on, alice, sent.id, ["TASK_STATE_INPUT_REQUIRED"]);
});

test("continuing another principal's task is TaskNotFoundError", async () => {
  const on = await colours();
  const open = await asked(on);
  const theirs = await on.rpc(
    "SendMessage",
    answer("a1", "red", open.id, open.contextId ?? ""),
    { as: bob },
  );
  expect(await faultName(theirs)).toBe("TaskNotFoundError");
});

test("continuing a task id that never existed is TaskNotFoundError", async () => {
  const on = await colours();
  const absent = await on.rpc(
    "SendMessage",
    answer("a1", "red", "01a00000-0000-7000-8000-000000000000", "c"),
    { as: alice },
  );
  expect(await faultName(absent)).toBe("TaskNotFoundError");
});

test("a free-text question takes the message's text as its answer", async () => {
  const on = await serve({
    agents: {
      support: asker({ question: "Your order id?", answer: "found it" }),
    },
    a2a: { expose: ONE },
  });
  const open = await asked(on);
  await task(
    await on.rpc(
      "SendMessage",
      answer("a1", "order-42", open.id, open.contextId ?? ""),
      { as: alice },
    ),
  );
  const done = await reaches(on, alice, open.id, ["TASK_STATE_COMPLETED"]);
  expect(done.artifacts?.[0]?.parts).toEqual([{ text: "found it" }]);
});
