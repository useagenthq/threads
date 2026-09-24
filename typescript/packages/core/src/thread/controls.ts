import type { z } from "zod";
import type {
  BranchId,
  PermissionMode,
  PermissionRule,
  Principal,
  ThreadId,
} from "../log";
import { knownEvents } from "../reduce";
import type { Result } from "../result";
import type { LogStore } from "../store";
import { cancelChildren } from "./cancel";
import {
  type Appended,
  answer,
  type ControlError,
  control,
  resolveParked,
  type SettingsChange,
} from "./control";
import { decide } from "./decide";
import {
  type BranchInfo,
  branchInfo,
  type PendingApproval,
  pendingApprovals,
} from "./pending";
import { readLog } from "./read";
import { cancel, setMode, setModel } from "./settings";
import { compact, setOutputStyle } from "./style";

type Controlled = Promise<Result<Appended, ControlError>>;

/** The control half of spec/api.json Thread: every method that appends names its principal. */
export type ThreadControl = {
  readonly branches: () => Promise<readonly BranchInfo[]>;
  readonly pendingApprovals: () => Promise<readonly PendingApproval[]>;
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
  readonly cancel: (principal: Principal) => Controlled;
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
      const rows = log.branches(threadId);
      return rows.ok ? rows.value.map(branchInfo) : [];
    },
    pendingApprovals: async () => {
      const current = readLog(log, branchId);
      if (!current.ok) return [];
      return pendingApprovals(
        knownEvents(current.value),
        current.value.fold,
        log.now(),
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
      if (done.ok) await cancelChildren(log, threadId, principal);
      return done;
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
