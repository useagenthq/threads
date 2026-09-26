import { afterEach, expect, test } from "bun:test";
import { alice, bob } from "../kit";
import { asker, talker } from "./agents";
import { faultName, message, reaches, serve, stopAll, task } from "./kit";

// CancelTask is the durable barrier on the task's thread. It answers the task in its current state,
// which becomes CANCELED once the barrier applies, and it never claims to undo what already
// happened. A task that has already ended cannot be cancelled at all.

afterEach(stopAll);

const ONE = { support: { description: "Support." } };

test("CancelTask on a running task answers the task and it ends CANCELED", async () => {
  const on = await serve({
    agents: {
      support: asker({ question: "Which colour?", answer: "done" }),
    },
    a2a: { expose: ONE },
  });
  const sent = await task(
    await on.rpc("SendMessage", message("m1", "hi"), { as: alice }),
  );
  const waiting = await reaches(on, alice, sent.id, [
    "TASK_STATE_INPUT_REQUIRED",
  ]);
  const answered = await on.rpc(
    "CancelTask",
    { id: waiting.id },
    { as: alice },
  );
  expect(answered.status).toBe(200);
  // The answer is the task, not a bare acknowledgement.
  expect((await task(answered)).id).toBe(waiting.id);
  const ended = await reaches(on, alice, waiting.id, ["TASK_STATE_CANCELED"]);
  expect(ended.status.state).toBe("TASK_STATE_CANCELED");
});

test("CancelTask on a task that has already ended is TaskNotCancelableError", async () => {
  const on = await serve({
    agents: { support: talker("all done") },
    a2a: { expose: ONE },
  });
  const sent = await task(
    await on.rpc("SendMessage", message("m1", "hi"), { as: alice }),
  );
  await reaches(on, alice, sent.id, ["TASK_STATE_COMPLETED"]);
  const refused = await on.rpc("CancelTask", { id: sent.id }, { as: alice });
  expect(await faultName(refused)).toBe("TaskNotCancelableError");
});

test("a cancelled task cannot be cancelled twice", async () => {
  const on = await serve({
    agents: {
      support: asker({ question: "Which colour?", answer: "done" }),
    },
    a2a: { expose: ONE },
  });
  const sent = await task(
    await on.rpc("SendMessage", message("m1", "hi"), { as: alice }),
  );
  await reaches(on, alice, sent.id, ["TASK_STATE_INPUT_REQUIRED"]);
  await on.rpc("CancelTask", { id: sent.id }, { as: alice });
  await reaches(on, alice, sent.id, ["TASK_STATE_CANCELED"]);
  const again = await on.rpc("CancelTask", { id: sent.id }, { as: alice });
  expect(await faultName(again)).toBe("TaskNotCancelableError");
});

test("another principal cannot cancel this caller's task", async () => {
  const on = await serve({
    agents: {
      support: asker({ question: "Which colour?", answer: "done" }),
    },
    a2a: { expose: ONE },
  });
  const sent = await task(
    await on.rpc("SendMessage", message("m1", "hi"), { as: alice }),
  );
  await reaches(on, alice, sent.id, ["TASK_STATE_INPUT_REQUIRED"]);
  const theirs = await on.rpc("CancelTask", { id: sent.id }, { as: bob });
  expect(await faultName(theirs)).toBe("TaskNotFoundError");
  // And it is still the caller's to cancel, so the refusal changed nothing.
  expect(
    (await on.rpc("CancelTask", { id: sent.id }, { as: alice })).status,
  ).toBe(200);
});

test("CancelTask works over the HTTP+JSON binding too", async () => {
  const on = await serve({
    agents: {
      support: asker({ question: "Which colour?", answer: "done" }),
    },
    a2a: { expose: ONE },
  });
  const sent = await task(
    await on.rpc("SendMessage", message("m1", "hi"), { as: alice }),
  );
  await reaches(on, alice, sent.id, ["TASK_STATE_INPUT_REQUIRED"]);
  const answered = await on.http("POST", `/tasks/${sent.id}:cancel`, {
    as: alice,
    body: {},
  });
  expect(answered.status).toBe(200);
  expect((await task(answered)).id).toBe(sent.id);
  expect(
    (await reaches(on, alice, sent.id, ["TASK_STATE_CANCELED"])).status.state,
  ).toBe("TASK_STATE_CANCELED");
});
