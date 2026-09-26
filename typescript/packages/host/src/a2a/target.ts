import {
  BranchId,
  knownEvents,
  type LogStore,
  ok,
  type Principal,
  type Result,
  type ThreadId,
  uuidv7,
} from "@threads/core/host";
import { type HostContext, samePin } from "../context";
import { fail, type StartFailure, type Target } from "../runs";
import type { ExposedAgent } from "./config";

// Where an exposed run goes: the thread its context derives, created on first use. The lookup and
// the create are one store transaction (rootOrCreate), so two hosts racing on one context make
// one thread rather than two.

export async function contextBranch(
  ctx: HostContext,
  log: LogStore,
  agent: ExposedAgent,
  principal: Principal,
  thread: ThreadId,
): Promise<Result<Target, StartFailure>> {
  // The authenticated caller answers this run's questions: ask_user is pinned.
  const pin = await agent.hosted.runner.started({ answerer: true });
  // The spec artifacts are durable before the event that names them is appended.
  await pin.put(ctx.storeFor(principal.tenant));
  const main = await log.rootOrCreate(
    thread,
    BranchId.parse(uuidv7(log.now())),
  );
  if (!main.ok) return fail("invalid_request", main.error.message);
  const read = await log.read(main.value);
  if (!read.ok) return fail("branch_not_runnable", read.error.message);
  const events = knownEvents(read.value);
  if (events.length > 0 && !(await samePin(events, agent.hosted)))
    return fail(
      "invalid_request",
      `this context's thread was not started with agent ${agent.hosted.key}'s current config`,
    );
  return ok({ threadId: thread, branchId: main.value, first: [pin.event] });
}
