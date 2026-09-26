import {
  type A2aFault,
  CancelTaskRequest,
  fault,
  GetTaskRequest,
  isTerminal,
  ListTasksRequest,
  type Method,
  SendMessageRequest,
  type SseFrame,
  SubscribeToTaskRequest,
} from "@threads/a2a/protocol";
import { assertNever, type Principal } from "@threads/core/host";
import type { HostContext } from "../context";
import type { ExposedAgent } from "./config";
import { rawMessage } from "./keys";
import type { Inbound } from "./parse";
import { isFault, sliceOf } from "./read";
import { sendMessage } from "./send";
import { taskOf } from "./state";
import { follow, parseCursor } from "./stream";
import { cancelTask, getTask, listTasks, locate } from "./tasks";
import { answer, type Envelope, itemOf, refuse, streamed } from "./wire";

// One operation, once the request is parsed and the caller is known. The request messages are
// re-parsed here from the same schemas rather than passed around loosely typed: inside this
// function the fields are the operation's own, not `unknown`.

export async function runOperation(
  ctx: HostContext,
  principal: Principal,
  inbound: Inbound,
  envelope: Envelope,
  agent: ExposedAgent,
  method: Method,
  params: unknown,
): Promise<Response> {
  switch (method) {
    case "SendMessage":
    case "SendStreamingMessage":
      return send(ctx, principal, inbound, envelope, agent, method, params);
    case "GetTask": {
      const got = await getTask(
        ctx,
        principal,
        GetTaskRequest.parse(params).id,
      );
      return isFault(got) ? refuse(envelope, got) : answer(envelope, got);
    }
    case "ListTasks": {
      const listed = await listTasks(
        ctx,
        principal,
        inbound.name,
        ListTasksRequest.parse(params),
      );
      return isFault(listed)
        ? refuse(envelope, listed)
        : answer(envelope, listed);
    }
    case "CancelTask": {
      const done = await cancelTask(
        ctx,
        principal,
        CancelTaskRequest.parse(params).id,
      );
      return isFault(done) ? refuse(envelope, done) : answer(envelope, done);
    }
    case "SubscribeToTask":
      return subscribe(
        ctx,
        principal,
        inbound,
        envelope,
        SubscribeToTaskRequest.parse(params).id,
      );
    default:
      return assertNever(method);
  }
}

async function send(
  ctx: HostContext,
  principal: Principal,
  inbound: Inbound,
  envelope: Envelope,
  agent: ExposedAgent,
  method: "SendMessage" | "SendStreamingMessage",
  params: unknown,
): Promise<Response> {
  const sent = await sendMessage(
    ctx,
    principal,
    inbound.name,
    agent,
    SendMessageRequest.parse(params),
    rawMessage(params),
  );
  if (isFault(sent)) return refuse(envelope, sent);
  if (method === "SendMessage") return answer(envelope, { task: sent.task });
  const at = sent.at;
  // A task refused before it existed has no log to follow: its one frame is the whole stream.
  return at === undefined
    ? streamed(one(itemOf(envelope)({ task: sent.task })))
    : streamed(follow(ctx, principal, at, undefined, itemOf(envelope)));
}

/**
 * A subscribe on a terminal task is UnsupportedOperationError, as the spec requires: a client that
 * finds its task already finished reads the result with GetTask. A resume starts after the frame
 * Last-Event-ID names, so it sees exactly the frames it has not seen.
 */
async function subscribe(
  ctx: HostContext,
  principal: Principal,
  inbound: Inbound,
  envelope: Envelope,
  taskId: string,
): Promise<Response> {
  const at = await locate(ctx, principal, taskId);
  if (isFault(at)) return refuse(envelope, at);
  const slice = await sliceOf(ctx, principal.tenant, at);
  if (slice === undefined)
    return refuse(envelope, fault("TaskNotFoundError", `no task ${taskId}`));
  if (isTerminal(taskOf(slice).status.state))
    return refuse(
      envelope,
      fault(
        "UnsupportedOperationError",
        `task ${taskId} has ended; read it with GetTask`,
      ),
    );
  const cursor = parseCursor(
    inbound.request.headers.get("last-event-id") ??
      inbound.url.searchParams.get("lastEventId"),
  );
  if (cursor !== undefined && isCursorFault(cursor))
    return refuse(envelope, cursor);
  return streamed(follow(ctx, principal, at, cursor, itemOf(envelope)));
}

function isCursorFault(
  cursor: { readonly seq: number; readonly k: number } | A2aFault,
): cursor is A2aFault {
  return isFault(cursor);
}

async function* one(data: string): AsyncGenerator<SseFrame> {
  yield { data };
}
