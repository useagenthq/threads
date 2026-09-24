import {
  type EventId,
  type KnownEvent,
  runEnd,
  type ThreadId,
} from "@threads/core/host";
import type { HostedAgent } from "../context";
import { failure, routeFailure } from "../errors";
import type { Call } from "../threads";
import { recordParts } from "./ai-sdk-decisions";
import {
  type AiSdkChatRequest,
  type AiSdkPart,
  AiSdkChatRequest as Body,
  ClientMessageId,
} from "./bodies";
import { routeAgent, UI_CODES, uiThread } from "./common";
import { CHAT_KEY, uiThreadId } from "./key";
import { LiveListener } from "./listener";
import { sseResponse } from "./sse";
import { startUiRun } from "./start";
import { uiFrames } from "./stream";

// The AI SDK routes (spec/schema/ui/README.md, "Routes"): POST /v1/ui/ai-sdk/{agent}, which
// useChat sends a new message or its approvals and answers to, and the reconnect route that
// useChat({resume: true}) calls.

export async function aiSdkChat(call: Call): Promise<Response> {
  const hosted = routeAgent(call);
  if (hosted instanceof Response) return hosted;
  const body = Body.safeParse(await call.body());
  if (!body.success) return failure("invalid_request", body.error.message);
  if (body.data.trigger === "regenerate-message")
    return failure(
      "invalid_request",
      "regenerate isn't supported; fork the thread to try again from an earlier point",
    );
  const threadId = uiThreadId(call.principal, hosted.key, body.data.id);
  const last = body.data.messages.at(-1);
  if (last?.role === "user") return submitted(call, hosted, threadId, last);
  if (last?.role === "assistant") return decided(call, threadId, last.parts);
  return failure(
    "invalid_request",
    "the last message must be the user's or the assistant's",
  );
}

type Message = AiSdkChatRequest["messages"][number];

async function submitted(
  call: Call,
  hosted: HostedAgent,
  threadId: ThreadId,
  message: Message,
): Promise<Response> {
  if (message.parts.some((p) => p.type !== "text"))
    return failure(
      "invalid_request",
      "only text parts are supported in a user message",
    );
  const id = ClientMessageId.safeParse(message.id);
  if (!id.success)
    return failure(
      "invalid_request",
      "a message id is 1 to 128 of A-Z a-z 0-9 _ . -",
    );
  const text = message.parts.map((p) => p.text ?? "").join("");
  if (text === "") return failure("invalid_request", "the message has no text");
  const listener = new LiveListener(call.ctx.hub, threadId);
  const started = await startUiRun(call.ctx, hosted, call.principal, threadId, {
    id: id.data,
    text,
  });
  if (!started.ok) {
    listener.stop();
    return routeFailure(UI_CODES, started.error);
  }
  const run = started.value;
  return sseResponse(
    "ai-sdk",
    uiFrames(
      call.ctx,
      {
        protocol: "ai-sdk",
        tenant: call.principal.tenant,
        thread: { id: run.thread_id, branch: run.branch_id },
        runId: run.run_id,
        ids: { threadId: run.thread_id, runId: run.run_id },
      },
      listener,
    ),
  );
}

/** Records the message's decisions and answers, then streams the run after the last of them. */
async function decided(
  call: Call,
  threadId: ThreadId,
  parts: readonly AiSdkPart[],
): Promise<Response> {
  const listener = new LiveListener(call.ctx.hub, threadId);
  const log = await uiThread(call, threadId);
  if (log === undefined) {
    listener.stop();
    return failure("not_found", "this chat has no thread yet");
  }
  const recorded = await recordParts(call.ctx, call.principal, log, parts);
  if (!recorded.ok) {
    listener.stop();
    return routeFailure(UI_CODES, recorded.error);
  }
  const after = (await uiThread(call, threadId))?.events;
  const last = after?.findLast((e) => recorded.value.includes(e.event_id));
  const run = last === undefined ? undefined : runOf(after ?? [], last.seq);
  if (last === undefined || run === undefined) {
    listener.stop();
    return new Response(null, { status: 204 });
  }
  return sseResponse(
    "ai-sdk",
    uiFrames(
      call.ctx,
      {
        protocol: "ai-sdk",
        tenant: call.principal.tenant,
        thread: { id: log.thread.id, branch: log.thread.branch },
        runId: run,
        ids: { threadId: log.thread.id, runId: run },
        after: { seq: last.seq, k: Number.POSITIVE_INFINITY },
      },
      listener,
    ),
  );
}

/** The run whose slice holds the event at `seq`: the latest user_input at or before it. */
function runOf(
  events: readonly KnownEvent[],
  seq: number,
): EventId | undefined {
  return events.findLast((e) => e.type === "user_input" && e.seq <= seq)
    ?.event_id;
}

/** GET {api}/{chat_id}/stream: the key's latest run from its start while it goes on, else 204. */
export async function aiSdkReconnect(call: Call): Promise<Response> {
  const hosted = routeAgent(call);
  if (hosted instanceof Response) return hosted;
  const key = call.params["chat_id"] ?? "";
  if (!CHAT_KEY.test(key))
    return failure(
      "invalid_request",
      "a chat id is 1 to 128 of A-Z a-z 0-9 _ . -",
    );
  const threadId = uiThreadId(call.principal, hosted.key, key);
  const listener = new LiveListener(call.ctx.hub, threadId);
  const log = await uiThread(call, threadId);
  const run = log?.events.findLast((e) => e.type === "user_input");
  const going =
    log !== undefined && run !== undefined && ongoing(log.events, run.event_id);
  if (log === undefined || run === undefined || !going) {
    listener.stop();
    return new Response(null, { status: 204 });
  }
  return sseResponse(
    "ai-sdk",
    uiFrames(
      call.ctx,
      {
        protocol: "ai-sdk",
        tenant: call.principal.tenant,
        thread: { id: log.thread.id, branch: log.thread.branch },
        runId: run.event_id,
        ids: { threadId: log.thread.id, runId: run.event_id },
      },
      listener,
    ),
  );
}

/** A run that has not ended and is not parked: its stream has more to send. */
function ongoing(events: readonly KnownEvent[], runId: EventId): boolean {
  return runEnd(events, runId).status === "running";
}
