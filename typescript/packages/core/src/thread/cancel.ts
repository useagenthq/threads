import { type KnownEvent, type Principal, ThreadId } from "../log";
import { ok } from "../result";
import type { LogStore } from "../store";
import { control } from "./control";

// Tree-wide cancellation (spec/schema/README.md, "Subagent cancellation and parking"; brief
// F7.6): every child a cancelled thread started and hasn't finished gets its own barrier,
// recursively, so each running descendant stops at its next step. A child whose turn is closed
// or already has a barrier gets nothing new; one with no thread yet is never started.

/** Barriers for every running child of `threadId`'s main branch, recursively. */
export async function cancelChildren(
  log: LogStore,
  threadId: ThreadId,
  principal: Principal,
): Promise<void> {
  const branch = log.mainBranch(threadId);
  const read = branch.ok ? log.read(branch.value) : undefined;
  if (read?.ok !== true) return;
  for (const [child, status] of read.value.fold.children)
    if (status === "running")
      await cancelTree(log, ThreadId.parse(child), principal);
}

/** A barrier on `threadId` when its open turn has none, then on its running children. */
export async function cancelTree(
  log: LogStore,
  threadId: ThreadId,
  principal: Principal,
  reason = "ancestor cancelled",
): Promise<boolean> {
  const branch = log.mainBranch(threadId);
  if (!branch.ok) return false;
  let barred = false;
  await control(log, branch.value, principal, (events) => {
    // Already stopped: a thread or tree cancel since its latest input. A child whose turn is
    // closed is barred too: it may be waiting on its own background children, and an idle tree
    // cancel ends that run and bars its wakes (rule 32).
    barred = cancelledSinceInput(events);
    if (barred)
      return { ok: false, error: { code: "not_found", message: "stopped" } };
    barred = true;
    return ok({
      record: {
        type: "cancel_requested",
        type_version: 1,
        critical: true,
        actor: { kind: "host", principal },
        data: { scope: "tree", reason },
      },
    });
  });
  await cancelChildren(log, threadId, principal);
  return barred;
}

/** A thread or tree cancel request since the latest user_input. */
function cancelledSinceInput(events: readonly KnownEvent[]): boolean {
  const input = events.findLastIndex((e) => e.type === "user_input");
  return events
    .slice(input + 1)
    .some((e) => e.type === "cancel_requested" && e.data.scope !== "turn");
}
