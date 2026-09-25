import {
  EventId,
  type KnownEvent,
  knownEvents,
  ThreadId,
} from "@threads/core/host";
import { failure } from "../errors";
import { runBranch } from "../subscribe";
import type { Call } from "../threads";
import { receiptsOf } from "./common";
import { type Cursor, eventFrames } from "./connection";
import { RunFacts } from "./facts";
import { isProtocol, type Protocol } from "./frame";
import { LiveListener } from "./listener";
import { runEvents } from "./session";
import { sseResponse } from "./sse";
import { uiFrames } from "./stream";

// GET /v1/threads/{thread_id}/runs/{run_id}/ui/{protocol}: a run's committed frames for custom
// clients, resumed by cursor (spec/schema/ui/README.md, "Cursor route"). No live text, so it is
// exact. AI SDK resumes frame by frame; AG-UI at the event boundary, as a new AG-UI run that
// opens with a snapshot through the cursor's event.

const CURSOR = /^(\d+):(\d+)$/;

export async function runFrames(
  call: Call,
  request: Request,
): Promise<Response> {
  const threadId = ThreadId.safeParse(call.params["thread_id"]);
  const runId = EventId.safeParse(call.params["run_id"]);
  const protocol = call.params["protocol"] ?? "";
  if (!threadId.success || !runId.success)
    return failure("invalid_request", "malformed thread_id or run_id");
  if (!isProtocol(protocol))
    return failure("not_found", `no UI protocol ${protocol}`);
  const { log } = await call.ctx.open(call.principal.tenant);
  const branch = await runBranch(log, threadId.data, runId.data);
  if (branch === undefined) return failure("not_found", `no run ${runId.data}`);
  const raw = request.headers.get("last-event-id") ?? call.query.get("after");
  const read = await log.read(branch);
  const events = read.ok ? knownEvents(read.value) : [];
  const after =
    raw === null ? undefined : cursorOf(events, protocol, runId.data, raw);
  if (after === "invalid")
    return failure(
      "invalid_cursor",
      `${raw} is not a frame of run ${runId.data}`,
    );
  const thread = { id: threadId.data, branch };
  const ids = { threadId: threadId.data, runId: runId.data };
  const plan = {
    protocol,
    tenant: call.principal.tenant,
    thread,
    runId: runId.data,
    ids,
  };
  const frames =
    after === undefined
      ? uiFrames(call.ctx, plan, new LiveListener(undefined, "", threadId.data))
      : uiFrames(
          call.ctx,
          protocol === "ai-sdk"
            ? { ...plan, after }
            : {
                ...plan,
                replay: after.seq,
                receipts: await receiptsOf(
                  call.ctx,
                  call.principal.tenant,
                  threadId.data,
                ),
              },
          new LiveListener(undefined, "", threadId.data),
        );
  return sseResponse(protocol, frames);
}

/** A cursor names a frame of the run: event `seq` of its slice, with `k` below its frame count. */
function cursorOf(
  events: readonly KnownEvent[],
  protocol: Protocol,
  runId: EventId,
  raw: string,
): Cursor | "invalid" {
  const m = CURSOR.exec(raw);
  if (m === null) return "invalid";
  const seq = Number(m[1]);
  const k = Number(m[2]);
  const facts = new RunFacts();
  for (const e of runEvents(events, runId)) {
    const count = eventFrames(protocol, e, facts).length;
    if (e.seq === seq) return k < count ? { seq, k } : "invalid";
  }
  return "invalid";
}
