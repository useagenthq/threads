import type { ThreadId } from "@threads/core/host";
import type { HostedAgent } from "../context";
import { failure, routeFailure } from "../errors";
import type { RunAccepted } from "../runs";
import type { Call } from "../threads";
import { applyResume } from "./ag-ui-resume";
import { type AgUiMessage, AgUiRunInput, ClientMessageId } from "./bodies";
import type { RunIds } from "./closing";
import { receiptsOf, routeAgent, UI_CODES, uiThread } from "./common";
import type { Chunk } from "./frame";
import { uiThreadId } from "./key";
import { LiveListener } from "./listener";
import { sseResponse } from "./sse";
import { findUiRun, startUiRun, type UserMessage } from "./start";
import { uiFrames } from "./stream";

// POST /v1/ui/ag-ui/{agent} (spec/schema/ui/README.md, "AG-UI bodies"): an AG-UI 1.0 run input.
// Its last user message starts a run, once; a retry (a new runId, the same message id) replays
// that run with a snapshot. Its resume entries answer the interrupts of a parked run, which then
// replays the same way.

export async function agUiRun(call: Call): Promise<Response> {
  const hosted = routeAgent(call);
  if (hosted instanceof Response) return hosted;
  const body = AgUiRunInput.safeParse(await call.body());
  if (!body.success) return failure("invalid_request", body.error.message);
  if ((body.data.tools ?? []).length > 0)
    return failure(
      "invalid_request",
      "frontend tools aren't supported; define tools on the agent",
    );
  const lastUser = body.data.messages.findLast((m) => m.role === "user");
  const message = lastUser === undefined ? undefined : userMessage(lastUser);
  if (typeof message === "string") return failure("invalid_request", message);
  const threadId = uiThreadId(call.principal, hosted.key, body.data.threadId);
  const ids = { threadId: body.data.threadId, runId: body.data.runId };
  const listener = new LiveListener(
    call.ctx.hub,
    call.principal.tenant,
    threadId,
  );
  const answered = await respond(call, hosted, threadId, {
    ids,
    message,
    resume: body.data.resume ?? [],
  });
  if (answered.stream === undefined) {
    listener.stop();
    return answered.response;
  }
  const { run, replay, extra } = answered.stream;
  return sseResponse(
    "ag-ui",
    uiFrames(
      call.ctx,
      {
        protocol: "ag-ui",
        tenant: call.principal.tenant,
        thread: { id: threadId, branch: run.branch_id },
        runId: run.run_id,
        ids,
        extra,
        ...(replay
          ? {
              replay: "head",
              receipts: await receiptsOf(
                call.ctx,
                call.principal.tenant,
                threadId,
              ),
            }
          : {}),
      },
      listener,
    ),
  );
}

type Input = {
  readonly ids: RunIds;
  readonly message: UserMessage | undefined;
  readonly resume: Parameters<typeof applyResume>[3];
};

type Answered =
  | { readonly response: Response; readonly stream?: undefined }
  | {
      readonly stream: {
        readonly run: RunAccepted;
        readonly replay: boolean;
        readonly extra: readonly Chunk[];
      };
    };

async function respond(
  call: Call,
  hosted: HostedAgent,
  threadId: ThreadId,
  input: Input,
): Promise<Answered> {
  const { message } = input;
  const found =
    message === undefined
      ? undefined
      : await findUiRun(
          call.ctx,
          hosted,
          call.principal.tenant,
          threadId,
          message,
        );
  if (found !== undefined && !found.ok)
    return { response: routeFailure(UI_CODES, found.error) };
  const newMessage = message !== undefined && found === undefined;
  let extra: readonly Chunk[] = [];
  if (input.resume.length > 0) {
    const log = await uiThread(call, threadId);
    if (log === undefined)
      return { response: failure("not_found", "this chat has no thread yet") };
    const { log: store } = await call.ctx.open(call.principal.tenant);
    const applied = await applyResume(
      call.ctx,
      call.principal,
      log,
      input.resume,
      {
        now: store.now(),
        newMessage,
      },
    );
    if (!applied.ok) return { response: routeFailure(UI_CODES, applied.error) };
    extra = applied.value;
    if (!newMessage) return replayLatest(call, threadId, found?.value, extra);
  }
  if (found !== undefined)
    return { stream: { run: found.value, replay: true, extra } };
  if (message === undefined)
    return {
      response: failure("invalid_request", "the input has no user message"),
    };
  const started = await startUiRun(
    call.ctx,
    hosted,
    call.principal,
    threadId,
    message,
  );
  return started.ok
    ? { stream: { run: started.value, replay: false, extra } }
    : { response: routeFailure(UI_CODES, started.error) };
}

/** After a resume: the run its message names, else the thread's latest, as a replay. */
async function replayLatest(
  call: Call,
  threadId: ThreadId,
  named: RunAccepted | undefined,
  extra: readonly Chunk[],
): Promise<Answered> {
  if (named !== undefined)
    return { stream: { run: named, replay: true, extra } };
  const log = await uiThread(call, threadId);
  const latest = log?.events.findLast((e) => e.type === "user_input");
  if (log === undefined || latest === undefined)
    return { response: failure("not_found", "this chat has no run to resume") };
  const run = {
    thread_id: threadId,
    branch_id: log.thread.branch,
    run_id: latest.event_id,
  };
  return { stream: { run, replay: true, extra } };
}

/** The message's id and text, or why it can't start a run. */
function userMessage(m: AgUiMessage): UserMessage | string {
  const id = ClientMessageId.safeParse(m.id);
  if (!id.success) return "a message id is 1 to 128 of A-Z a-z 0-9 _ . -";
  const content = m.content ?? "";
  if (typeof content === "string")
    return content === ""
      ? "the message has no text"
      : { id: id.data, text: content };
  if (content.some((p) => p.type !== "text"))
    return "only text parts are supported in a user message";
  const text = content.map((p) => p.text ?? "").join("");
  return text === "" ? "the message has no text" : { id: id.data, text };
}
