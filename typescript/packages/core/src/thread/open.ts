import type { Thread } from "../agent/result";
import { openStore, type Store } from "../agent/sqlite";
import { BranchId, type EventId, type ThreadId } from "../log";
import { knownEvents, reduce } from "../reduce";
import { err, ok, type Result } from "../result";
import type { Sandbox, SnapshotData } from "../sandbox/protocol";
import type { LogStore } from "../store";
import { uuidv7 } from "../store/encode";
import { type LogError, logError, type VerifiedLog } from "../verify";
import { forkBranch, type KnowledgePolicy } from "./fork";
import { type SaveCaseOptions, type SavedCase, saveCase } from "./save-case";

// openThread() (spec/api.json, ): a handle for inspection and control that reads
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
export type ThreadHandle = Thread & {
  readonly timeline: () => Promise<Result<Timeline, LogError>>;
  readonly forkPoints: () => Promise<readonly ForkPoint[]>;
  readonly fork: (
    point: EventId | ForkPoint,
    options?: ForkOptions,
  ) => Promise<Result<ThreadHandle, LogError>>;
  readonly saveCase: (
    name: string,
    options: SaveCaseOptions,
  ) => Promise<Result<SavedCase, LogError>>;
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

/** A branch's log as a reader sees it; a failure other than an unsupported line is log_corrupt. */
function readLog(
  log: LogStore,
  branchId: BranchId,
): Result<VerifiedLog, LogError> {
  const read = log.read(branchId);
  if (read.ok) return read;
  const { code, message, seq } = read.error;
  return code === "unsupported_format" || code === "unsupported_critical_event"
    ? read
    : err(logError("log_corrupt", message, seq));
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
): Promise<Result<ThreadHandle, LogError>> {
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
      if (forkOptions.mode === "stub" && sandbox?.info.egress !== "enforced")
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
    saveCase: async (name, caseOptions) =>
      saveCase(log, branchId, name, caseOptions, {
        artifacts,
        sandbox: options.sandbox,
        points: await points(),
      }),
  });
}
