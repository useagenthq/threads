import { type Principal, ThreadId } from "../log";
import { turnEvents } from "../loop/turn";
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
): Promise<void> {
  const branch = log.mainBranch(threadId);
  if (!branch.ok) return;
  await control(log, branch.value, principal, (events, writer) => {
    const barred = turnEvents(events).some(
      (e) => e.type === "cancel_requested",
    );
    // Nothing to stop: the child's turn is closed or already has a barrier.
    if (!writer.chain.fold.turnOpen || barred)
      return { ok: false, error: { code: "not_found", message: "no turn" } };
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
}
