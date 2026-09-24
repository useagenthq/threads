import { openThread, type Thread } from "@threads/core";
import {
  BranchId,
  CallId,
  knownEvents,
  type Principal,
  ThreadId,
  Uuid,
} from "@threads/core/host";
import type { z } from "zod";
import type { HostContext } from "./context";
import { failure, json, routeFailure } from "./errors";
import {
  Answer,
  ApprovalDecision,
  ForkRequest,
  ModeChange,
  ParkedResolution,
  SettingsChange,
} from "./schemas";

// The thread routes of openapi.json: each is a thin wrapper over the Thread method in its
// x-api, opened in the caller's tenant (another tenant's thread is not_found) at a branch that
// must be the thread's, with the authenticated principal passed through.

export type Call = {
  readonly ctx: HostContext;
  readonly principal: Principal;
  readonly params: Readonly<Record<string, string>>;
  readonly query: URLSearchParams;
  readonly body: () => Promise<unknown>;
};

type Opened = { readonly thread: Thread; readonly principal: Principal };

/**
 * The route's thread. A thread that can't be read answers with its read error when the route
 * declares it (openapi.json x-error-codes), else not_found; log_corrupt is every route's.
 */
async function opened(
  call: Call,
  withSandbox = false,
  readErrors: ReadonlySet<string> = CORRUPT,
): Promise<Opened | Response> {
  const threadId = ThreadId.safeParse(call.params["thread_id"]);
  const branch = call.query.get("branch_id");
  const branchId = branch === null ? undefined : BranchId.safeParse(branch);
  if (!threadId.success || branchId?.success === false)
    return failure("invalid_request", "malformed thread_id or branch_id");
  const store = call.ctx.storeFor(call.principal.tenant);
  const sandbox = withSandbox
    ? await sandboxOf(call, threadId.data)
    : undefined;
  const thread = await openThread(store, threadId.data, {
    ...(branchId === undefined ? {} : { branchId: branchId.data }),
    ...(sandbox === undefined ? {} : { sandbox }),
  });
  if (!thread.ok)
    return readErrors.has(thread.error.code)
      ? failure(thread.error.code, thread.error.message)
      : failure("not_found", `no thread ${threadId.data}`);
  return { thread: thread.value, principal: call.principal };
}

const CORRUPT: ReadonlySet<string> = new Set(["log_corrupt"]);
/** Every read error of spec/api.json, for the routes that declare them all. */
const READ_ERRORS: ReadonlySet<string> = new Set([
  "log_corrupt",
  "unsupported_format",
  "unsupported_critical_event",
]);

async function sandboxOf(call: Call, threadId: ThreadId) {
  const { log } = await call.ctx.open(call.principal.tenant);
  const main = log.mainBranch(threadId);
  const read = main.ok ? log.read(main.value) : undefined;
  if (read?.ok !== true) return undefined;
  return call.ctx.agentOf(knownEvents(read.value))?.runner.sandbox;
}

async function parsed<T>(
  call: Call,
  schema: z.ZodType<T>,
): Promise<T | Response> {
  const body = schema.safeParse(await call.body());
  return body.success
    ? body.data
    : failure("invalid_request", body.error.message);
}

/**
 * After a control appended, the branch continues from the log (a resumed or a cancel). A
 * subagent's thread continues through its root: the parent parked on it re-runs it and resumes.
 */
async function resume(call: Call, thread: Thread): Promise<void> {
  const { log } = await call.ctx.open(call.principal.tenant);
  let at = { id: thread.id, branch: thread.branch };
  for (;;) {
    const read = log.read(at.branch);
    if (!read.ok) return;
    const events = knownEvents(read.value);
    const started = events.find((e) => e.type === "thread_started");
    const parent =
      started?.type === "thread_started" ? started.data.parent : undefined;
    if (parent?.relation === "subagent") {
      at = { id: parent.thread_id, branch: parent.branch_id };
      continue;
    }
    const hosted = call.ctx.agentOf(events);
    if (hosted === undefined) return;
    void call.ctx.resume(hosted, call.principal.tenant, call.principal, at);
    return;
  }
}

export async function timeline(call: Call): Promise<Response> {
  const o = await opened(call, false, READ_ERRORS);
  if (o instanceof Response) return o;
  const t = await o.thread.timeline();
  return t.ok ? json(200, t.value) : failure(t.error.code, t.error.message);
}

export async function branches(call: Call): Promise<Response> {
  const o = await opened(call);
  return o instanceof Response ? o : json(200, await o.thread.branches());
}

export async function forkPoints(call: Call): Promise<Response> {
  const o = await opened(call);
  return o instanceof Response ? o : json(200, await o.thread.forkPoints());
}

const FORK_CODES = [
  "not_found",
  "invalid_request",
  "sandbox_required",
  "no_snapshot_boundary",
  "snapshot_expired",
  "snapshot_missing",
  "snapshot_restore_failed",
  "snapshot_manifest_mismatch",
  "resource_unknown",
  "egress_policy_unsupported",
];

export async function fork(call: Call): Promise<Response> {
  const o = await opened(call, true);
  if (o instanceof Response) return o;
  const body = await parsed(call, ForkRequest);
  if (body instanceof Response) return body;
  const child = await o.thread.fork(body.event_id, {
    ...(body.mode === undefined ? {} : { mode: body.mode }),
    ...(body.knowledge === undefined ? {} : { knowledge: body.knowledge }),
  });
  if (child.ok)
    return json(201, {
      thread_id: child.value.id,
      branch_id: child.value.branch,
    });
  const code = FORK_CODES.includes(child.error.code)
    ? child.error.code
    : "invalid_request";
  return failure(code, child.error.message);
}

export async function approvals(call: Call): Promise<Response> {
  const o = await opened(call, false, READ_ERRORS);
  if (o instanceof Response) return o;
  const pending = await o.thread.pendingApprovals();
  return pending.ok
    ? json(200, pending.value)
    : failure(pending.error.code, pending.error.message);
}

const DECIDE_CODES = [
  "forbidden",
  "not_found",
  "invalid_request",
  "approval_mismatch",
  "approval_expired",
  "approval_duplicate",
  "branch_busy",
];

/** forbidden unless the caller has approval authority on the thread's root run. */
async function authority(
  call: Call,
  thread: Thread,
): Promise<Response | undefined> {
  const may = await call.ctx.mayApprove(
    call.principal.tenant,
    thread.id,
    call.principal,
  );
  return may
    ? undefined
    : failure("forbidden", "this principal may not approve for this run");
}

export async function decide(call: Call): Promise<Response> {
  const challenge = Uuid.safeParse(call.params["challenge_id"]);
  if (!challenge.success)
    return failure("invalid_request", "malformed challenge_id");
  const o = await opened(call);
  if (o instanceof Response) return o;
  const body = await parsed(call, ApprovalDecision);
  if (body instanceof Response) return body;
  const refused = await authority(call, o.thread);
  if (refused !== undefined) return refused;
  const done =
    body.decision === "grant"
      ? await o.thread.approve(challenge.data, call.principal, {
          ...(body.remember_rule === undefined
            ? {}
            : { rememberRule: body.remember_rule }),
        })
      : await o.thread.deny(challenge.data, call.principal, {
          ...(body.reason === undefined ? {} : { reason: body.reason }),
        });
  if (!done.ok) return routeFailure(DECIDE_CODES, done.error);
  await resume(call, o.thread);
  return json(200, done.value);
}

export async function answer(call: Call): Promise<Response> {
  const callId = CallId.safeParse(call.params["call_id"]);
  if (!callId.success) return failure("invalid_request", "malformed call_id");
  const o = await opened(call);
  if (o instanceof Response) return o;
  const body = await parsed(call, Answer);
  if (body instanceof Response) return body;
  const done = await o.thread.answer(callId.data, body.answer, call.principal);
  if (!done.ok)
    return routeFailure(
      [
        "forbidden",
        "not_found",
        "invalid_request",
        "no_open_question",
        "invalid_answer",
        "branch_busy",
      ],
      done.error,
    );
  await resume(call, o.thread);
  return json(200, done.value);
}

export async function resolveParked(call: Call): Promise<Response> {
  const key = call.params["effect_key"] ?? "";
  const o = await opened(call);
  if (o instanceof Response) return o;
  const body = await parsed(call, ParkedResolution);
  if (body instanceof Response) return body;
  // Either resolution accepts duplicate risk, so it needs approval authority.
  const refused = await authority(call, o.thread);
  if (refused !== undefined) return refused;
  const done = await o.thread.resolveParked(
    key,
    body.resolution,
    call.principal,
  );
  if (!done.ok)
    return routeFailure(
      [
        "forbidden",
        "not_found",
        "invalid_request",
        "not_parked",
        "branch_busy",
      ],
      done.error,
    );
  await resume(call, o.thread);
  return json(200, done.value);
}

export async function cancel(call: Call): Promise<Response> {
  const o = await opened(call);
  if (o instanceof Response) return o;
  const done = await o.thread.cancel(call.principal);
  if (!done.ok)
    return routeFailure(["forbidden", "not_found", "branch_busy"], done.error);
  await resume(call, o.thread);
  return json(200, done.value);
}

const SETTING_CODES = [
  "forbidden",
  "not_found",
  "invalid_request",
  "invalid_transition",
  "branch_busy",
];

export async function setModel(call: Call): Promise<Response> {
  const o = await opened(call);
  if (o instanceof Response) return o;
  const body = await parsed(call, SettingsChange);
  if (body instanceof Response) return body;
  const done = await o.thread.setModel(
    {
      model: body.model,
      ...(body.model_params === undefined
        ? {}
        : { model_params: body.model_params }),
      ...(body.reasoning_carryover === undefined
        ? {}
        : { reasoning_carryover: body.reasoning_carryover }),
    },
    call.principal,
  );
  return done.ok
    ? json(200, done.value)
    : routeFailure(SETTING_CODES, done.error);
}

export async function setMode(call: Call): Promise<Response> {
  const o = await opened(call);
  if (o instanceof Response) return o;
  const body = await parsed(call, ModeChange);
  if (body instanceof Response) return body;
  const done = await o.thread.setMode(body.mode, call.principal);
  return done.ok
    ? json(200, done.value)
    : routeFailure(SETTING_CODES, done.error);
}
