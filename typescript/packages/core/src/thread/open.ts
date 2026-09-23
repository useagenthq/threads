import type { z } from "zod";
import type { ThreadRef } from "../agent/result";
import { openStore, type Store } from "../agent/sqlite";
import {
  BranchId,
  type EventId,
  type PermissionMode,
  type PermissionRule,
  type Principal,
  type ThreadId,
} from "../log";
import { knownEvents, type Projections, projections, reduce } from "../reduce";
import { err, ok, type Result } from "../result";
import type { Sandbox, SnapshotData } from "../sandbox/protocol";
import type { LogStore } from "../store";
import { uuidv7 } from "../store/encode";
import { type LogError, logError, type VerifiedLog } from "../verify";
import { cancelChildren } from "./cancel";
import {
  type Appended,
  answer,
  type ControlError,
  control,
  decide,
  resolveParked,
  type SettingsChange,
} from "./control";
import { forkBranch, type KnowledgePolicy } from "./fork";
import {
  type BranchInfo,
  branchInfo,
  type PendingApproval,
  pendingApprovals,
} from "./pending";
import { readLog } from "./read";
import { type SaveCaseOptions, type SavedCase, saveCase } from "./save-case";
import { cancel, setMode, setModel } from "./settings";
import { type ThreadUsage, usageMethods } from "./usage";

// openThread() (spec/api.json): a handle for inspection and control that reads
// through the store and needs no agent in memory.

/** host-api ForkPoint: an eligible snapshot event, the value fork() takes. */
export type ForkPoint = {
  readonly branch_id: BranchId;
  readonly seq: number;
  readonly event_id: EventId;
  readonly snapshot: SnapshotData;
};

/** host-api Timeline: the resolved chain, oldest first, with the fork points marked. */
export type Timeline = {
  readonly thread_id: ThreadId;
  readonly branch_id: BranchId;
  readonly entries: readonly {
    readonly event: VerifiedLog["events"][number]["event"];
    readonly fork_point: boolean;
  }[];
};

export type ForkOptions = {
  readonly mode?: "live" | "stub";
  readonly knowledge?: KnowledgePolicy;
};

/** spec/api.json Thread: the plain handle plus its inspection and fork methods. */
export type Thread = ThreadRef & {
  readonly timeline: () => Promise<Result<Timeline, LogError>>;
  readonly forkPoints: () => Promise<readonly ForkPoint[]>;
  readonly fork: (
    point: EventId | ForkPoint,
    options?: ForkOptions,
  ) => Promise<Result<Thread, LogError>>;
  readonly saveCase: (
    name: string,
    options: SaveCaseOptions,
  ) => Promise<Result<SavedCase, LogError>>;
  /** The latest todo list. */
  readonly todos: () => Promise<Projections["todos"]>;
  /** One entry per spawned child, running until its agent_finished. */
  readonly children: () => Promise<Projections["children"]>;
} & ThreadUsage &
  ThreadControl;

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
};

export type OpenThreadOptions = {
  readonly branchId?: BranchId;
  /** The adapter fork() restores with; its provider must match the snapshot's. */
  readonly sandbox?: Sandbox;
};

const HOLDER = `fork-${crypto.randomUUID()}`;

/** Eligible snapshot events of the resolved chain, with the branch whose segment holds each. */
export function forkPoints(
  log: VerifiedLog,
  now: number,
): readonly ForkPoint[] {
  const eligible = new Set(
    reduce(log, now).fork_points.map((p) => p.snapshot_event_id),
  );
  return knownEvents(log).flatMap((event) =>
    event.type === "snapshot" && eligible.has(event.event_id)
      ? [
          {
            branch_id: event.branch_id,
            seq: event.seq,
            event_id: event.event_id,
            snapshot: event.data,
          },
        ]
      : [],
  );
}

/** The listed branch of this thread: ready or inspection-only, never forking or failed. */
function listedBranch(
  log: LogStore,
  threadId: ThreadId,
  branchId: BranchId | undefined,
): Result<BranchId, LogError> {
  const branch =
    branchId === undefined ? log.mainBranch(threadId) : ok(branchId);
  const state = branch.ok ? log.branchState(branch.value) : branch;
  const listed =
    state.ok && (state.value === "ready" || state.value === "inspection_only");
  return branch.ok && listed
    ? branch
    : err(logError("not_found", `no thread ${threadId}`));
}

export async function openThread(
  store: Store,
  threadId: ThreadId,
  options: OpenThreadOptions = {},
): Promise<Result<Thread, LogError>> {
  const { log, artifacts } = await openStore(store);
  const branch = listedBranch(log, threadId, options.branchId);
  if (!branch.ok) return branch;
  const branchId = branch.value;
  const read = readLog(log, branchId);
  if (!read.ok) return read;
  if (read.value.segments[0]?.header.thread_id !== threadId)
    return err(logError("not_found", `no thread ${threadId}`));

  const points = async (): Promise<readonly ForkPoint[]> => {
    const current = readLog(log, branchId);
    return current.ok ? forkPoints(current.value, log.now()) : [];
  };
  return ok({
    id: threadId,
    branch: branchId,
    store,
    timeline: async () => {
      const current = readLog(log, branchId);
      if (!current.ok) return current;
      const marked = new Set(
        forkPoints(current.value, log.now()).map((p) => p.event_id),
      );
      return ok({
        thread_id: threadId,
        branch_id: branchId,
        entries: current.value.events.map(({ event }) => ({
          event,
          fork_point: marked.has(event.event_id),
        })),
      });
    },
    forkPoints: points,
    fork: async (point, forkOptions = {}) => {
      const sandbox = options.sandbox;
      if (sandbox === undefined)
        return err(
          logError(
            "sandbox_required",
            "fork restores into a sandbox; open the thread with one",
          ),
        );
      if (forkOptions.mode === "stub" && sandbox.info.egress !== "enforced")
        return err(
          logError(
            "egress_policy_unsupported",
            "stub mode needs a sandbox that enforces deny-all egress",
          ),
        );
      const child = BranchId.parse(uuidv7(log.now()));
      const forked = await forkBranch(log, sandbox, {
        parent: branchId,
        point: typeof point === "string" ? point : point.event_id,
        child,
        knowledge: forkOptions.knowledge ?? "pinned",
        holderId: HOLDER,
      });
      if (!forked.ok) return forked;
      return openThread(store, threadId, { ...options, branchId: child });
    },
    todos: async () => {
      const current = readLog(log, branchId);
      return current.ok ? projections(current.value).todos : [];
    },
    children: async () => {
      const current = readLog(log, branchId);
      return current.ok ? projections(current.value).children : [];
    },
    ...usageMethods(log, branchId),
    ...controls(log, threadId, branchId),
    saveCase: async (name, caseOptions) =>
      saveCase(log, branchId, name, caseOptions, {
        artifacts,
        sandbox: options.sandbox,
        points: await points(),
      }),
  });
}

function controls(
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
  };
}
