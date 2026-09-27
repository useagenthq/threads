import {
  type A2aFault,
  fault,
  isSettled,
  isTerminal,
  type Message,
  textOf,
} from "@threadsai/a2a/protocol";
import { openThread } from "threadsai";
import {
  type Principal,
  principalKey,
  storeConnection,
  writing,
} from "threadsai/host";
import type { HostContext } from "../context";
import { answerQuestion } from "../decisions";
import { A2A_SEND, a2aTask, findReceipt, insertReceipt } from "../receipts";
import { sendKey } from "./keys";
import { type Accepted, located, sliceOf } from "./read";
import { taskOf } from "./state";

// A message with a taskId continues that task, and only while it is INPUT_REQUIRED: its text
// answers the run's open ask_user, strictly to that question's options. The continuation carries a
// receipt under the same key form as a send, so a retried answer returns the task in its current
// state rather than an error — a caller in doubt about us is never punished for asking again.

export async function continueTask(
  ctx: HostContext,
  principal: Principal,
  name: string,
  message: Message,
  hash: string,
): Promise<Accepted | A2aFault> {
  const taskId = message.taskId;
  if (taskId === undefined) throw new Error("a continuation carries a taskId");
  const at = {
    tenant: principal.tenant,
    operation: A2A_SEND,
    key: sendKey(principal, name, message.messageId),
  } as const;
  const { db } = await storeConnection(ctx.store);
  // The receipt first here too: a retried answer is answered from what is stored.
  const prior = await findReceipt(db, at);
  if (!prior.ok) return fault("InternalError", prior.error.message);
  if (prior.value !== undefined)
    return prior.value.body_hash === hash
      ? located(ctx, principal, {
          thread: prior.value.thread_id,
          branch: prior.value.branch_id,
          runId: prior.value.run_id,
        })
      : fault(
          "InvalidParamsError",
          `messageId ${message.messageId} was already used with a different message`,
        );
  // Resolved only through the caller's own receipts: another principal's task is not found, and
  // is indistinguishable from one that never existed.
  const owned = await a2aTask(
    db,
    principal.tenant,
    principalKey(principal),
    taskId,
  );
  if (!owned.ok) return fault("InternalError", owned.error.message);
  const receipt = owned.value;
  if (receipt === undefined)
    return fault("TaskNotFoundError", `no task ${taskId}`);
  const found = {
    thread: receipt.thread_id,
    branch: receipt.branch_id,
    runId: receipt.run_id,
  };
  const slice = await sliceOf(ctx, principal.tenant, found);
  if (slice === undefined)
    return fault("InternalError", `task ${taskId} could not be read`);
  const state = taskOf(slice).status.state;
  if (isTerminal(state))
    return fault(
      "UnsupportedOperationError",
      `task ${taskId} has ended and cannot be continued`,
    );
  if (!isSettled(state))
    return fault(
      "UnsupportedOperationError",
      "task is still working; wait or cancel it",
    );
  const question = slice.question;
  if (question === undefined)
    return fault("InternalError", `task ${taskId} has no open question`);
  const opened = await openThread(
    ctx.storeFor(principal.tenant),
    found.thread,
    {
      branchId: found.branch,
    },
  );
  if (!opened.ok) return fault("InternalError", opened.error.message);
  const answered = await answerQuestion(
    ctx,
    principal,
    opened.value,
    question.callId,
    textOf(message.parts),
  );
  if (!answered.ok)
    return fault(
      answered.error.code === "invalid_answer"
        ? "InvalidParamsError"
        : "UnsupportedOperationError",
      answered.error.message,
    );
  // The answer is recorded; the receipt follows, so a later retry of this messageId replays it.
  await writing(db, (tx) =>
    insertReceipt(
      tx,
      at,
      {
        principal_key: principalKey(principal),
        body_hash: hash,
        thread_id: found.thread,
        branch_id: found.branch,
        run_id: found.runId,
      },
      Date.now(),
    ),
  );
  return located(ctx, principal, found);
}
