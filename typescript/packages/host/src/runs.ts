import {
  BranchId,
  canonicalize,
  type EventDraft,
  type EventId,
  err,
  knownEvents,
  type LogError,
  type LogStore,
  ok,
  type Principal,
  principalKey,
  type Result,
  sha256Hex,
  storeConnection,
  ThreadId,
  uuidv7,
} from "@threads/core/host";
import { type HostContext, type HostedAgent, samePin } from "./context";
import type { Failure } from "./errors";
import { findReceipt, insertReceipt, type Keyed } from "./receipts";
import type { StartRunRequest } from "./schemas";

// Host.startRun (POST /v1/runs): the user_input and its idempotency receipt commit in one
// transaction, then the run proceeds in the host. run_id is that user_input's event_id.

export type RunAccepted = {
  readonly thread_id: ThreadId;
  readonly branch_id: BranchId;
  readonly run_id: EventId;
};

export type StartRunCode =
  | "forbidden"
  | "invalid_request"
  | "not_found"
  | "branch_busy"
  | "branch_not_runnable"
  | "idempotency_key_reused"
  | "idempotency_key_principal_mismatch";

type StartFailure = Failure & { readonly code: StartRunCode };
type Started = Result<RunAccepted, StartFailure>;

const fail = (
  code: StartRunCode,
  message: string,
): { readonly ok: false; readonly error: StartFailure } =>
  err({ code, message });

export async function startRun(
  ctx: HostContext,
  request: StartRunRequest,
  principal: Principal,
  idempotencyKey: string,
): Promise<Started> {
  const hosted = ctx.agents.get(request.agent);
  if (hosted === undefined)
    return fail("not_found", `no agent ${request.agent}`);
  const text = canonicalize(request);
  if (!text.ok) return fail("invalid_request", text.error.message);
  const binding = {
    principal_key: principalKey(principal),
    body_hash: sha256Hex(text.value),
  };
  const at = { tenant: principal.tenant, key: idempotencyKey };
  const { db } = await storeConnection(ctx.store);
  const prior = replayed(findReceipt(db, at), binding);
  if (prior !== undefined) return prior;
  const accepted = await accept(ctx, hosted, request, principal, {
    at,
    binding,
  });
  // Another request took the key between our read and our commit: answer as a replay.
  if (!accepted.ok && accepted.error.code === "idempotency_key_reused")
    return replayed(findReceipt(db, at), binding) ?? accepted;
  if (!accepted.ok) return accepted;
  const { thread_id, branch_id, run_id } = accepted.value;
  void ctx.resume(
    hosted,
    principal.tenant,
    principal,
    { id: thread_id, branch: branch_id },
    run_id,
  );
  return accepted;
}

type Binding = { readonly principal_key: string; readonly body_hash: string };

/** A stored receipt answers the request: the same one replays, anything else is refused. */
function replayed(
  found: ReturnType<typeof findReceipt>,
  binding: Binding,
): Started | undefined {
  if (!found.ok) return fail("invalid_request", found.error.message);
  const receipt = found.value;
  if (receipt === undefined) return undefined;
  // A different principal learns nothing about the other principal's receipt (F12.12).
  if (receipt.principal_key !== binding.principal_key)
    return fail(
      "idempotency_key_principal_mismatch",
      "this Idempotency-Key belongs to another principal",
    );
  if (receipt.body_hash !== binding.body_hash)
    return fail(
      "idempotency_key_reused",
      "this Idempotency-Key was used with another request",
    );
  const { thread_id, branch_id, run_id } = receipt;
  return ok({ thread_id, branch_id, run_id });
}

async function accept(
  ctx: HostContext,
  hosted: HostedAgent,
  request: StartRunRequest,
  principal: Principal,
  key: { readonly at: Keyed; readonly binding: Binding },
): Promise<Started> {
  const { log } = await ctx.open(principal.tenant);
  const target = await branchFor(log, hosted, request);
  if (!target.ok) return target;
  const { threadId, branchId, first } = target.value;
  const holder = `host-${crypto.randomUUID()}`;
  const writer = log.acquire(branchId, holder);
  if (!writer.ok) return fail(leaseCode(writer.error), writer.error.message);
  try {
    if (writer.value.chain.fold.turnOpen)
      return fail("branch_busy", "the branch is in the middle of a turn");
    const { db } = await storeConnection(ctx.store);
    const drafts = [...first, input(request, principal)];
    let lost = false;
    const appended = writer.value.append(drafts, (added) => {
      const run = added.at(-1);
      if (run?.kind !== "event") throw new Error("user_input is a known event");
      const won = insertReceipt(
        db,
        key.at,
        {
          ...key.binding,
          thread_id: threadId,
          branch_id: branchId,
          run_id: run.event.event_id,
        },
        log.now(),
      );
      lost = !won;
      return won
        ? ok(undefined)
        : err({ code: "invalid_request", message: "the key was taken" });
    });
    if (lost) return fail("idempotency_key_reused", "the key was taken");
    if (!appended.ok) return fail("invalid_request", appended.error.message);
    const run = appended.value.at(-1);
    if (run?.kind !== "event") throw new Error("user_input is a known event");
    return ok({
      thread_id: threadId,
      branch_id: branchId,
      run_id: run.event.event_id,
    });
  } finally {
    writer.value.release();
  }
}

function input(request: StartRunRequest, principal: Principal): EventDraft {
  return {
    type: "user_input",
    type_version: 1,
    critical: true,
    actor: { kind: "user", principal },
    data: {
      source: "api",
      ...(typeof request.input === "string"
        ? { text: request.input }
        : { content: [...request.input] }),
      ...(request.budget === undefined ? {} : { budget: request.budget }),
    },
  };
}

type Target = {
  readonly threadId: ThreadId;
  readonly branchId: BranchId;
  /** A new thread's thread_started, appended with its first input. */
  readonly first: readonly EventDraft[];
};

/** The branch to continue (it must be the thread's and started by this agent), or a new thread. */
async function branchFor(
  log: LogStore,
  hosted: HostedAgent,
  request: StartRunRequest,
): Promise<Result<Target, StartFailure>> {
  if (request.thread_id === undefined) {
    const threadId = ThreadId.parse(uuidv7(log.now()));
    const branchId = BranchId.parse(uuidv7(log.now()));
    const made = log.createBranch(threadId, branchId);
    if (!made.ok) return fail("invalid_request", made.error.message);
    return ok({ threadId, branchId, first: [await hosted.runner.started()] });
  }
  const threadId = request.thread_id;
  const branchId = request.branch_id ?? mainOf(log, threadId);
  const listed = log.branches(threadId);
  if (
    branchId === undefined ||
    !listed.ok ||
    !listed.value.some((b) => b.branch_id === branchId)
  )
    return fail("not_found", `no thread ${threadId}`);
  const read = log.read(branchId);
  if (!read.ok) return fail("branch_not_runnable", read.error.message);
  if (!(await samePin(knownEvents(read.value), hosted)))
    return fail(
      "invalid_request",
      `thread ${threadId} was not started with this agent's config`,
    );
  return ok({ threadId, branchId, first: [] });
}

function mainOf(log: LogStore, threadId: ThreadId): BranchId | undefined {
  const main = log.mainBranch(threadId);
  return main.ok ? main.value : undefined;
}

function leaseCode(error: LogError): StartRunCode {
  if (error.code === "branch_busy") return "branch_busy";
  if (error.code === "branch_not_found") return "not_found";
  return "branch_not_runnable";
}
