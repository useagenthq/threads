import type { ThreadRef } from "../agent/result";
import type { OpenStore } from "../agent/sqlite";
import { BranchId, type EventId, type ThreadId } from "../log";
import { knownEvents, type Projections, projections, reduce } from "../reduce";
import { err, ok, type Result } from "../result";
import type { Sandbox, SnapshotData } from "../sandbox/protocol";
import { uuidv7 } from "../store/encode";
import { type LogError, logError, type VerifiedLog } from "../verify";
import { controls, type ThreadControl } from "./controls";
import { type ExportedBundle, exportBundle } from "./export";
import { forkBranch, type KnowledgePolicy } from "./fork";
import { type ReadError, readLog } from "./read";
import { type ReplayError, replay } from "./replay";
import { type SaveCaseOptions, type SavedCase, saveCase } from "./save-case";
import { type ThreadUsage, usageMethods } from "./usage";

// The Thread handle (spec/api.json Thread): every method reads or appends through the store when
// called, so making one does no I/O. A run's result carries one; openThread checks the branch
// first, then makes one.

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
  readonly timeline: () => Promise<Result<Timeline, ReadError>>;
  readonly forkPoints: () => Promise<readonly ForkPoint[]>;
  readonly fork: (
    point: EventId | ForkPoint,
    options?: ForkOptions,
  ) => Promise<Result<Thread, LogError>>;
  readonly saveCase: (
    name: string,
    options: SaveCaseOptions,
  ) => Promise<Result<SavedCase, LogError>>;
  /**
   * Writes this branch's chain and every artifact it names to `path` as a portable bundle,
   * which `importThread` stores anywhere. `path` must not exist.
   */
  readonly export: (path: string) => Promise<Result<ExportedBundle, LogError>>;
  /** The latest todo list. */
  readonly todos: () => Promise<Projections["todos"]>;
  /** One entry per spawned child, running until its agent_finished. */
  readonly children: () => Promise<Projections["children"]>;
  /**
   * Re-renders every recorded model request from the log and checks it byte for byte: no model
   * or tool calls, no appends. The first failure names the request's seq. Run it in CI to prove
   * an upgrade still reproduces your threads.
   */
  readonly replay: () => Promise<Result<void, ReplayError>>;
} & ThreadUsage &
  ThreadControl;

export type HandleOptions = {
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

/** The Thread handle on `ref`'s branch, reading through the store `opened` already holds. */
export function threadHandle(
  opened: OpenStore,
  ref: ThreadRef,
  options: HandleOptions = {},
): Thread {
  const { log, artifacts } = opened;
  const { id: threadId, branch: branchId } = ref;
  const points = async (): Promise<readonly ForkPoint[]> => {
    const current = await readLog(log, branchId);
    return current.ok ? forkPoints(current.value, log.now()) : [];
  };
  return {
    id: threadId,
    branch: branchId,
    store: ref.store,
    timeline: async () => {
      const current = await readLog(log, branchId);
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
      return ok(threadHandle(opened, { ...ref, branch: child }, options));
    },
    todos: async () => {
      const current = await readLog(log, branchId);
      return current.ok ? projections(current.value).todos : [];
    },
    children: async () => {
      const current = await readLog(log, branchId);
      return current.ok ? projections(current.value).children : [];
    },
    replay: async () => replay(log, artifacts, branchId),
    ...usageMethods(log, threadId, branchId),
    ...controls(log, threadId, branchId),
    saveCase: async (name, caseOptions) =>
      saveCase(log, branchId, name, caseOptions, {
        artifacts,
        sandbox: options.sandbox,
        points: await points(),
      }),
    export: async (path) => exportBundle(log, artifacts, branchId, path),
  };
}
