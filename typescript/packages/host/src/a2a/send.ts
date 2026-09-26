import {
  type A2aFault,
  fault,
  isFilePart,
  type Message,
  type SendMessageRequest,
  type Task,
  textOf,
} from "@threads/a2a/protocol";
import {
  type EventDraft,
  type Principal,
  principalKey,
  storeConnection,
  uuidv7,
} from "@threads/core/host";
import type { HostContext } from "../context";
import { A2A_SEND, findReceipt } from "../receipts";
import { start } from "../runs";
import type { ExposedAgent } from "./config";
import { continueTask } from "./continue";
import {
  a2aThreadId,
  bodyHash,
  claimedHops,
  claims,
  MAX_HOPS,
  messageIdTooLong,
  rejectedTaskId,
  sendKey,
} from "./keys";
import { type Accepted, located, openRun } from "./read";
import { contextBranch } from "./target";

// SendMessage and SendStreamingMessage. The receipt lookup is the first thing that happens, before
// any contextId is minted, so a retry that carries no contextId still answers the original task
// and the original context. The receipt row and the run's user_input commit in one transaction,
// which is what makes a duplicate delivery — even to two hosts on one store — one run.

export async function sendMessage(
  ctx: HostContext,
  principal: Principal,
  name: string,
  agent: ExposedAgent,
  request: SendMessageRequest,
  /** The caller's own `message` JSON, which is what the body hash is taken over. */
  raw: unknown,
): Promise<Accepted | A2aFault> {
  const { message } = request;
  if (request.configuration?.taskPushNotificationConfig !== undefined)
    return fault(
      "PushNotificationNotSupportedError",
      "this agent does not support push notifications; follow the task with SubscribeToTask or GetTask",
    );
  const bad = unusable(message);
  if (bad !== undefined) return bad;
  const hash = bodyHash(name, raw);
  if (hash === undefined)
    return fault("InvalidParamsError", "the message is not representable JSON");
  if (message.taskId !== undefined)
    return continueTask(ctx, principal, name, message, hash);
  return startTask(ctx, principal, name, agent, message, hash);
}

/** What we refuse before looking at anything stored. */
function unusable(message: Message): A2aFault | undefined {
  if (message.parts.some(isFilePart))
    return fault(
      "ContentTypeNotSupportedError",
      "this agent takes text and JSON parts; a file part is not supported",
    );
  if (messageIdTooLong(message.messageId))
    return fault("InvalidParamsError", "messageId is at most 255 bytes");
  return undefined;
}

async function startTask(
  ctx: HostContext,
  principal: Principal,
  name: string,
  agent: ExposedAgent,
  message: Message,
  hash: string,
): Promise<Accepted | A2aFault> {
  const at = {
    tenant: principal.tenant,
    operation: A2A_SEND,
    key: sendKey(principal, name, message.messageId),
  } as const;
  const { db } = await storeConnection(ctx.store);
  const found = await findReceipt(db, at);
  if (!found.ok) return fault("InternalError", found.error.message);
  // Before any contextId is minted: a retry without one answers the original task and context.
  if (found.value !== undefined) {
    const stored = found.value;
    if (stored.body_hash !== hash)
      return fault(
        "InvalidParamsError",
        `messageId ${message.messageId} was already used with a different message`,
      );
    return located(ctx, principal, {
      thread: stored.thread_id,
      branch: stored.branch_id,
      runId: stored.run_id,
    });
  }
  const claim = claims(message.metadata);
  const hops = claimedHops(claim);
  if (hops !== undefined && hops >= MAX_HOPS)
    return { task: tooDeep(principal, name, message), at: undefined };
  const contextId = message.contextId ?? uuidv7(Date.now());
  const thread = a2aThreadId(principal, name, contextId);
  if (await openRun(ctx, principal, thread))
    return fault(
      "UnsupportedOperationError",
      "a task is already working in this context",
    );
  const started = await start(ctx, agent.hosted, principal, {
    at,
    binding: { principal_key: principalKey(principal), body_hash: hash },
    keyName: `messageId ${message.messageId}`,
    target: (log) => contextBranch(ctx, log, agent, principal, thread),
    input: input(principal, agent, message, contextId),
  });
  if (!started.ok)
    return fault(
      started.error.code === "branch_busy"
        ? "UnsupportedOperationError"
        : "InvalidParamsError",
      started.error.message,
    );
  return located(ctx, principal, {
    thread: started.value.thread_id,
    branch: started.value.branch_id,
    runId: started.value.run_id,
  });
}

/**
 * A claimed hop count too deep. Nothing is stored and no run starts, so the refusal costs the
 * caller everything and us nothing; the id is derived from the request, so a retry is answered
 * identically. A claim is never authority, so this is the only thing a claim can decide.
 */
function tooDeep(principal: Principal, name: string, message: Message): Task {
  const id = rejectedTaskId(principal, name, message.messageId);
  return {
    id,
    contextId: message.contextId ?? id,
    status: {
      state: "TASK_STATE_REJECTED",
      message: {
        messageId: id,
        role: "ROLE_AGENT",
        parts: [{ text: "call chain too deep" }],
      },
    },
  };
}

/** The run's user_input: the authenticated principal as its actor, the claim as a claim. */
function input(
  principal: Principal,
  agent: ExposedAgent,
  message: Message,
  contextId: string,
): EventDraft {
  const claim = claims(message.metadata);
  return {
    type: "user_input",
    type_version: 1,
    critical: true,
    actor: { kind: "user", principal },
    data: {
      source: "api",
      text: textOf(message.parts),
      budget: agent.budget,
      a2a: {
        message_id: message.messageId,
        context_id: contextId,
        ...(message.taskId === undefined ? {} : { task_id: message.taskId }),
        ...(claim === undefined ? {} : { claims: claim }),
      },
    },
  };
}
