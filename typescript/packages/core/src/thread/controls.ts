import type { z } from "zod";
import { stubForkRef } from "../agent/frozen-stubs";
import type {
  BranchId,
  PermissionMode,
  PermissionRule,
  Principal,
  ThreadId,
} from "../log";
import { knownEvents } from "../reduce";
import { ok, type Result } from "../result";
import type { LogStore } from "../store";
import type { ListedBranch } from "../store/fork-reads";
import { cancelChildren } from "./cancel";
import {
  type Appended,
  answer,
  type ControlError,
  control,
  resolveParked,
  type SettingsChange,
} from "./control";
import { type CancelAccepted, writeControlItem } from "./control-items";
import { decide } from "./decide";
import {
  type BranchInfo,
  branchInfo,
  memberView,
  type PendingApproval,
  pendingApprovals,
} from "./pending";
import { type ReadError, readLog } from "./read";
import { cancel, setMode, setModel } from "./settings";
import { compact, setOutputStyle } from "./style";

type Controlled = Promise<Result<Appended, ControlError>>;

/** The control half of spec/api.json Thread: every method that appends names its principal. */
export type ThreadControl = {
  readonly branches: () => Promise<readonly BranchInfo[]>;
  readonly pendingApprovals: () => Promise<
    Result<readonly PendingApproval[], ReadError>
  >;
  readonly approve: (
    challengeId: string,
    principal: Principal,
    options?: { readonly rememberRule?: z.infer<typeof PermissionRule> },
  ) => Controlled;
  readonly deny: (
    challengeId: string,
    principal: Principal,
    options?: { readonly reason?: string },
  ) => Controlled;
  readonly answer: (
    callId: string,
    answer: string | readonly string[],
    principal: Principal,
  ) => Controlled;
  readonly resolveParked: (
    effectKey: string,
    resolution: "assume_done" | "assume_not_done",
    principal: Principal,
  ) => Controlled;
  /**
   * The durable barrier, tree-wide. Appended here when this process may take the branch
   * (`Appended`); when another process holds it, a durable control item its holder applies at
   * its next step boundary (`CancelAccepted`), never `branch_busy`.
   */
  readonly cancel: (
    principal: Principal,
  ) => Promise<Result<Appended | CancelAccepted, ControlError>>;
  readonly setModel: (
    settings: SettingsChange,
    principal: Principal,
  ) => Controlled;
  readonly setMode: (
    mode: z.infer<typeof PermissionMode>,
    principal: Principal,
  ) => Controlled;
  /**
   * While the thread is idle, asks its next run to summarize everything up to now before its
   * first model request. The summary, or why it failed, is in the timeline.
   */
  readonly compact: (
    principal: Principal,
    options?: { readonly instructions?: string },
  ) => Controlled;
  /** While the thread is idle, switches later replies to one of the agent's output styles. */
  readonly setOutputStyle: (name: string, principal: Principal) => Controlled;
};

export function controls(
  log: LogStore,
  threadId: ThreadId,
  branchId: BranchId,
): ThreadControl {
  return {
    branches: async () => {
      const rows = await log.branches(threadId);
      if (!rows.ok) return [];
      const listed: BranchInfo[] = [];
      for (const row of rows.value)
        listed.push(branchInfo(row, await stubbed(log, row)));
      return listed;
    },
    pendingApprovals: async () => {
      const current = await readLog(log, branchId);
      if (!current.ok) return current;
      const events = knownEvents(current.value);
      const read = async (branch: BranchId) => {
        const other = await log.read(branch);
        return other.ok ? knownEvents(other.value) : undefined;
      };
      return ok(
        pendingApprovals(
          events,
          current.value.fold,
          log.now(),
          await memberView(events, read),
        ),
      );
    },
    approve: (id, principal, options = {}) =>
      control(
        log,
        branchId,
        principal,
        decide(id, principal, log.now(), {
          grant: true,
          ...(options.rememberRule === undefined
            ? {}
            : { rememberRule: options.rememberRule }),
        }),
      ),
    deny: (id, principal, options = {}) =>
      control(
        log,
        branchId,
        principal,
        decide(id, principal, log.now(), {
          grant: false,
          ...(options.reason === undefined ? {} : { reason: options.reason }),
        }),
      ),
    answer: (callId, text, principal) =>
      control(log, branchId, principal, answer(callId, text, principal)),
    resolveParked: (key, resolution, principal) =>
      control(
        log,
        branchId,
        principal,
        resolveParked(key, resolution, principal),
      ),
    cancel: async (principal) => {
      const done = await control(log, branchId, principal, cancel(principal));
      // Authority is checked here, before either is written: applying an item never checks
      // again. control() refuses a principal of another tenant before it touches the branch.
      if (!done.ok && done.error.code !== "branch_busy") return done;
      const accepted = done.ok
        ? undefined
        : await writeControlItem(
            log.driver,
            log.tenant,
            threadId,
            principal,
            "cancel",
            log.now(),
          );
      await cancelChildren(log, threadId, principal);
      return accepted === undefined ? done : ok(accepted);
    },
    setModel: (settings, principal) =>
      control(log, branchId, principal, setModel(settings, principal)),
    setMode: (mode, principal) =>
      control(log, branchId, principal, setMode(mode, principal)),
    compact: (principal, options = {}) =>
      control(
        log,
        branchId,
        principal,
        compact(principal, options.instructions),
        {
          idle: true,
        },
      ),
    setOutputStyle: (name, principal) =>
      control(log, branchId, principal, setOutputStyle(name, principal), {
        idle: true,
      }),
  };
}

/**
 * Whether a listed branch runs stubbed: its resolved chain holds a fork that froze a stub script.
 * A root branch never does, so the common case reads nothing.
 */
async function stubbed(log: LogStore, row: ListedBranch): Promise<boolean> {
  if (row.parent_branch_id === null) return false;
  const read = await log.read(row.branch_id);
  return read.ok && stubForkRef(knownEvents(read.value)) !== undefined;
}
