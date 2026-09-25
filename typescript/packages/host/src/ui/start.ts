import {
  BranchId,
  type EventDraft,
  EventId,
  inputText,
  knownEvents,
  type LogStore,
  ok,
  type Principal,
  principalKey,
  type Result,
  type Store,
  storeConnection,
  type ThreadId,
  uiBodyHash,
  uuidv7,
} from "@threads/core/host";
import { type HostContext, type HostedAgent, samePin } from "../context";
import { findReceipt, UI_RUN } from "../receipts";
import {
  fail,
  type Started,
  type StartFailure,
  start,
  type Target,
} from "../runs";

// A UI route's run start (spec/schema/ui/README.md, "Idempotency"): the last user message
// starts a run on the key's thread, once. Its id keys the run: a retry finds it by the `ui`
// receipt, or as the event id of one of the thread's user_inputs, and replays it; a different
// text under the same id is idempotency_key_reused.

export type UserMessage = { readonly id: string; readonly text: string };

export async function startUiRun(
  ctx: HostContext,
  hosted: HostedAgent,
  principal: Principal,
  threadId: ThreadId,
  message: UserMessage,
): Promise<Started> {
  const found = await findUiRun(
    ctx,
    hosted,
    principal.tenant,
    threadId,
    message,
  );
  if (found !== undefined) return found;
  const bodyHash = uiBodyHash(hosted.name, message.text);
  if (bodyHash === undefined)
    return fail("invalid_request", "the message text is not valid Unicode");
  return start(ctx, hosted, principal, {
    at: {
      tenant: principal.tenant,
      operation: UI_RUN,
      key: uiKey(threadId, message.id),
    },
    binding: { principal_key: principalKey(principal), body_hash: bodyHash },
    keyName: `message ${message.id}`,
    target: (log) =>
      uiTarget(log, ctx.storeFor(principal.tenant), hosted, threadId),
    input: input(principal, message),
  });
}

/** A `ui` receipt's key: the thread and the client's message id. */
export function uiKey(threadId: string, messageId: string): string {
  return `${threadId}:${messageId}`;
}

/**
 * The run a message already started, found by its `ui` receipt, else as the event id of one of
 * the thread's user_inputs (a client that took its ids from a snapshot); undefined for a new
 * message. A match with another text is idempotency_key_reused.
 */
export async function findUiRun(
  ctx: HostContext,
  hosted: HostedAgent,
  tenant: string,
  threadId: ThreadId,
  message: UserMessage,
): Promise<Started | undefined> {
  const { db } = await storeConnection(ctx.store);
  const receipt = await findReceipt(db, {
    tenant,
    operation: UI_RUN,
    key: uiKey(threadId, message.id),
  });
  if (!receipt.ok) return fail("invalid_request", receipt.error.message);
  const found = receipt.value;
  if (found !== undefined)
    return found.body_hash === uiBodyHash(hosted.name, message.text)
      ? ok({
          thread_id: threadId,
          branch_id: found.branch_id,
          run_id: found.run_id,
        })
      : reused(message);
  return runByEventId(ctx, tenant, threadId, message);
}

function reused(message: UserMessage): Started {
  return fail(
    "idempotency_key_reused",
    `message ${message.id} was sent with another text`,
  );
}

async function runByEventId(
  ctx: HostContext,
  tenant: string,
  threadId: ThreadId,
  message: UserMessage,
): Promise<Started | undefined> {
  const id = EventId.safeParse(message.id);
  if (!id.success) return undefined;
  const { log } = await ctx.open(tenant);
  const branch = await log.mainBranch(threadId);
  if (!branch.ok) return undefined;
  const read = await log.read(branch.value);
  if (!read.ok) return undefined;
  const run = knownEvents(read.value).find(
    (e) => e.type === "user_input" && e.event_id === id.data,
  );
  if (run?.type !== "user_input") return undefined;
  if (inputText(run) !== message.text) return reused(message);
  return ok({
    thread_id: threadId,
    branch_id: run.branch_id,
    run_id: run.event_id,
  });
}

function input(principal: Principal, message: UserMessage): EventDraft {
  return {
    type: "user_input",
    type_version: 1,
    critical: true,
    actor: { kind: "user", principal },
    data: { source: "api", text: message.text, client_message_id: message.id },
  };
}

/**
 * The key's thread: continued when it exists, else created with its derived id. The lookup and
 * the create are one store transaction, so requests racing on a new key, in this process or
 * another, make one thread.
 */
async function uiTarget(
  log: LogStore,
  store: Store,
  hosted: HostedAgent,
  threadId: ThreadId,
): Promise<Result<Target, StartFailure>> {
  // The signed-in caller can answer this run's questions: ask_user is pinned.
  const pin = await hosted.runner.started({ answerer: true });
  // The spec artifacts are durable before first, which names them, is appended.
  await pin.put(store);
  const first = [pin.event];
  const main = await log.rootOrCreate(
    threadId,
    BranchId.parse(uuidv7(log.now())),
  );
  if (!main.ok) return fail("invalid_request", main.error.message);
  const read = await log.read(main.value);
  if (!read.ok) return fail("branch_not_runnable", read.error.message);
  const events = knownEvents(read.value);
  if (events.length > 0 && !(await samePin(events, hosted)))
    return fail(
      "invalid_request",
      `this chat's thread was not started with agent ${hosted.key}'s current config`,
    );
  return ok({ threadId, branchId: main.value, first });
}
