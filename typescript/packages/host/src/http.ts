import { EventId, Int, Principal, ThreadId } from "@threads/core/host";
import type { HostContext } from "./context";
import { failure, json, routeFailure } from "./errors";
import { startRun } from "./runs";
import { IdempotencyKey, StartRunRequest } from "./schemas";
import { type SseMessage, subscribe } from "./subscribe";
import * as threads from "./threads";

// The typed HTTP API (spec/schema/host-api/openapi.json). Every /v1 route authenticates the
// caller as a principal first (absent authenticate: 401) and passes that principal to the
// library call, never the local-operator default.

export type Authenticate = (request: Request) => Promise<Principal | null>;

type Handler = (call: threads.Call, request: Request) => Promise<Response>;
type Route = {
  readonly method: string;
  readonly path: RegExp;
  readonly handler: Handler;
};

const T = "/v1/threads/(?<thread_id>[^/]+)";

const ROUTES: readonly Route[] = [
  { method: "POST", path: /^\/v1\/runs$/, handler: runs },
  {
    method: "GET",
    path: new RegExp(`^${T}/runs/(?<run_id>[^/]+)/events$`),
    handler: events,
  },
  {
    method: "GET",
    path: new RegExp(`^${T}/timeline$`),
    handler: threads.timeline,
  },
  {
    method: "GET",
    path: new RegExp(`^${T}/branches$`),
    handler: threads.branches,
  },
  {
    method: "GET",
    path: new RegExp(`^${T}/fork-points$`),
    handler: threads.forkPoints,
  },
  { method: "POST", path: new RegExp(`^${T}/forks$`), handler: threads.fork },
  {
    method: "GET",
    path: new RegExp(`^${T}/approvals$`),
    handler: threads.approvals,
  },
  {
    method: "POST",
    path: new RegExp(`^${T}/approvals/(?<challenge_id>[^/]+)$`),
    handler: threads.decide,
  },
  {
    method: "POST",
    path: new RegExp(`^${T}/questions/(?<call_id>[^/]+)/answer$`),
    handler: threads.answer,
  },
  {
    method: "POST",
    path: new RegExp(`^${T}/parked/(?<effect_key>[^/]+)/resolve$`),
    handler: threads.resolveParked,
  },
  {
    method: "POST",
    path: new RegExp(`^${T}/cancel$`),
    handler: threads.cancel,
  },
  {
    method: "POST",
    path: new RegExp(`^${T}/settings$`),
    handler: threads.setModel,
  },
  { method: "POST", path: new RegExp(`^${T}/mode$`), handler: threads.setMode },
];

/** A path parameter's text, or undefined when a %-escape is malformed (client input). */
function pathParam(raw: string): string | undefined {
  try {
    return decodeURIComponent(raw);
  } catch {
    return undefined;
  }
}

export async function api(
  ctx: HostContext,
  authenticate: Authenticate | undefined,
  request: Request,
): Promise<Response> {
  const url = new URL(request.url);
  const found = ROUTES.map((r) => ({ r, m: r.path.exec(url.pathname) })).find(
    ({ r, m }) => m !== null && r.method === request.method,
  );
  const who = authenticate === undefined ? null : await authenticate(request);
  const principal = Principal.safeParse(who);
  if (who === null || !principal.success)
    return failure("unauthenticated", "the request is not authenticated");
  if (found === undefined)
    return failure("not_found", `no route ${url.pathname}`);
  const params: Record<string, string> = {};
  for (const [k, v] of Object.entries(found.m?.groups ?? {})) {
    const decoded = pathParam(v);
    if (decoded === undefined)
      return failure("invalid_request", `malformed escape in ${k}`);
    params[k] = decoded;
  }
  const call: threads.Call = {
    ctx,
    principal: principal.data,
    params,
    query: url.searchParams,
    body: async () => {
      try {
        return await request.json();
      } catch {
        return undefined;
      }
    },
  };
  return found.r.handler(call, request);
}

const RUN_CODES = [
  "forbidden",
  "invalid_request",
  "not_found",
  "branch_busy",
  "branch_not_runnable",
  "idempotency_key_reused",
  "idempotency_key_principal_mismatch",
];

async function runs(call: threads.Call, request: Request): Promise<Response> {
  const key = IdempotencyKey.safeParse(request.headers.get("idempotency-key"));
  if (!key.success)
    return failure("invalid_request", "Idempotency-Key is required");
  const body = StartRunRequest.safeParse(await call.body());
  if (!body.success) return failure("invalid_request", body.error.message);
  const accepted = await startRun(
    call.ctx,
    body.data,
    call.principal,
    key.data,
  );
  return accepted.ok
    ? json(202, accepted.value)
    : routeFailure(RUN_CODES, accepted.error);
}

async function events(call: threads.Call, request: Request): Promise<Response> {
  const threadId = ThreadId.safeParse(call.params["thread_id"]);
  const runId = EventId.safeParse(call.params["run_id"]);
  const after = Int.safeParse(
    Number(
      call.query.get("after_seq") ?? request.headers.get("last-event-id") ?? 0,
    ),
  );
  if (!threadId.success || !runId.success || !after.success)
    return failure(
      "invalid_request",
      "malformed thread_id, run_id or after_seq",
    );
  const stream = await subscribe(
    call.ctx,
    threadId.data,
    runId.data,
    call.principal,
    after.data,
  );
  if (!stream.ok) return failure(stream.error.code, stream.error.message);
  return new Response(sse(stream.value), {
    status: 200,
    headers: {
      "content-type": "text/event-stream",
      "cache-control": "no-cache",
    },
  });
}

/** SSE framing: committed events carry their seq as the id; the result closes the stream. */
function sse(messages: AsyncIterable<SseMessage>): ReadableStream<Uint8Array> {
  const utf8 = new TextEncoder();
  const iterator = messages[Symbol.asyncIterator]();
  return new ReadableStream({
    async pull(controller) {
      const next = await iterator.next();
      if (next.done === true) {
        controller.close();
        return;
      }
      const m = next.value;
      const id = m.kind === "event" ? `id: ${m.event.seq}\n` : "";
      controller.enqueue(utf8.encode(`${id}data: ${JSON.stringify(m)}\n\n`));
    },
    async cancel() {
      await iterator.return?.();
    },
  });
}
