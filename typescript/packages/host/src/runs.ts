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
  type Store,
  sha256Hex,
  storeConnection,
  ThreadId,
  uuidv7,
} from "@threads/core/host";
import { type HostContext, type HostedAgent, samePin } from "./context";
import type { Failure } from "./errors";
import { findReceipt, insertReceipt, type Keyed, START_RUN } from "./receipts";
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

export type StartFailure = Failure & { readonly code: StartRunCode };
export type Started = Result<RunAccepted, StartFailure>;

export const fail = (
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
  return start(ctx, hosted, principal, {
    at: { tenant: principal.tenant, operation: START_RUN, key: idempotencyKey },
    binding: {
      principal_key: principalKey(principal),
      body_hash: sha256Hex(text.value),
    },
    keyName: "this Idempotency-Key",
    target: (log) =>
      branchFor(log, ctx.storeFor(principal.tenant), hosted, request),
    input: input(request, principal),
  });
}

/** How one run starts: its receipt, the branch it goes to and its user_input. */
export type RunPlan = {
  readonly at: Keyed;
  readonly binding: Binding;
  /** What the key is called in a refusal, e.g. "this Idempotency-Key". */
  readonly keyName: string;
  readonly target: (log: LogStore) => Promise<Result<Target, StartFailure>>;
  readonly input: EventDraft;
};

/**
 * The receipt answers a replay; otherwise the user_input and its receipt commit in one
 * transaction and the run proceeds in the host.
 */
export async function start(
  ctx: HostContext,
  hosted: HostedAgent,
  principal: Principal,
  plan: RunPlan,
): Promise<Started> {
  const { db } = await storeConnection(ctx.store);
  const prior = replayed(await findReceipt(db, plan.at), plan);
  if (prior !== undefined) return prior;
  const accepted = await accept(ctx, principal, plan);
  // Another request took the key between our read and our commit: answer as a replay.
  if (!accepted.ok && accepted.error.code === "idempotency_key_reused")
    return replayed(await findReceipt(db, plan.at), plan) ?? accepted;
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

export type Binding = {
  readonly principal_key: string;
  readonly body_hash: string;
};

/** A stored receipt answers the request: the same one replays, anything else is refused. */
function replayed(
  found: Awaited<ReturnType<typeof findReceipt>>,
  { binding, keyName }: Pick<RunPlan, "binding" | "keyName">,
): Started | undefined {
  if (!found.ok) return fail("invalid_request", found.error.message);
  const receipt = found.value;
  if (receipt === undefined) return undefined;
  // A different principal learns nothing about the other principal's receipt (F12.12).
  if (receipt.principal_key !== binding.principal_key)
    return fail(
      "idempotency_key_principal_mismatch",
      `${keyName} belongs to another principal`,
    );
  if (receipt.body_hash !== binding.body_hash)
    return fail(
      "idempotency_key_reused",
      `${keyName} was used with another request`,
    );
  const { thread_id, branch_id, run_id } = receipt;
  return ok({ thread_id, branch_id, run_id });
}

async function accept(
  ctx: HostContext,
  principal: Principal,
  plan: RunPlan,
): Promise<Started> {
  const { log } = await ctx.open(principal.tenant);
  const target = await plan.target(log);
  if (!target.ok) return target;
  const { threadId, branchId, first } = target.value;
  const holder = `host-${crypto.randomUUID()}`;
  const writer = await log.acquire(branchId, holder);
  if (!writer.ok) return fail(leaseCode(writer.error), writer.error.message);
  try {
    if (writer.value.chain.fold.turnOpen)
      return fail("branch_busy", "the branch is in the middle of a turn");
    // A branch opens with its thread_started, once: a racing request may have written it.
    const empty = writer.value.chain.events.length === 0;
    const drafts = [...(empty ? first : []), plan.input];
    // Set by each attempt of the append: only the last one's counts.
    let lost = false;
    const appended = await writer.value.append(drafts, async (added, tx) => {
      const run = added.at(-1);
      if (run?.kind !== "event") throw new Error("user_input is a known event");
      const won = await insertReceipt(
        tx,
        plan.at,
        {
          ...plan.binding,
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
    await writer.value.release();
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

export type Target = {
  readonly threadId: ThreadId;
  readonly branchId: BranchId;
  /** A new thread's thread_started, appended with its first input. */
  readonly first: readonly EventDraft[];
};

/** The branch to continue (it must be the thread's and started by this agent), or a new thread. */
async function branchFor(
  log: LogStore,
  store: Store,
  hosted: HostedAgent,
  request: StartRunRequest,
): Promise<Result<Target, StartFailure>> {
  if (request.thread_id === undefined) {
    const threadId = ThreadId.parse(uuidv7(log.now()));
    const branchId = BranchId.parse(uuidv7(log.now()));
    const made = await log.createBranch(threadId, branchId);
    if (!made.ok) return fail("invalid_request", made.error.message);
    // An authenticated caller can answer this run's questions: ask_user is pinned.
    const pin = await hosted.runner.started({ answerer: true });
    // The spec artifacts are durable before first, which names them, is appended.
    await pin.put(store);
    return ok({ threadId, branchId, first: [pin.event] });
  }
  const threadId = request.thread_id;
  const branchId = request.branch_id ?? (await mainOf(log, threadId));
  const listed = await log.branches(threadId);
  if (
    branchId === undefined ||
    !listed.ok ||
    !listed.value.some((b) => b.branch_id === branchId)
  )
    return fail("not_found", `no thread ${threadId}`);
  const read = await log.read(branchId);
  if (!read.ok) return fail("branch_not_runnable", read.error.message);
  if (!(await samePin(knownEvents(read.value), hosted)))
    return fail(
      "invalid_request",
      `thread ${threadId} was not started with this agent's config`,
    );
  return ok({ threadId, branchId, first: [] });
}

async function mainOf(
  log: LogStore,
  threadId: ThreadId,
): Promise<BranchId | undefined> {
  const main = await log.mainBranch(threadId);
  return main.ok ? main.value : undefined;
}

function leaseCode(error: LogError): StartRunCode {
  if (error.code === "branch_busy") return "branch_busy";
  if (error.code === "branch_not_found") return "not_found";
  return "branch_not_runnable";
}
