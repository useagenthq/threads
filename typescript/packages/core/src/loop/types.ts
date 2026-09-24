import type { z } from "zod";
import type { EventOf, Fold } from "../fold/state";
import type { LoopExtension } from "../hooks/types";
import type {
  BranchId,
  EventId,
  InjectedData,
  KnownEvent,
  Policy,
  Principal,
  ResultPart,
  ThreadId,
  ToolSpec,
  Usage,
} from "../log";
import type { LookupResult, Model } from "../model";
import type { Result } from "../result";
import type { Stale } from "../sandbox/protocol";
import type { BudgetLedger } from "../store/budget";
import type { DynamicChoice } from "../team/dynamic";

/**
 * RunResult failed.error.code: host-api RunErrorCode (spec/schema/host-api), closed. A test
 * pins this list to the schema.
 */
export type RunErrorCode =
  | "model_unavailable"
  | "context_exhausted"
  | "max_output"
  | "max_turns"
  | "output_invalid"
  | "input_denied"
  | "stop_hook_limit"
  | "model_error"
  | "content_unsupported"
  | "continuation_unsupported"
  | "transport_fence_unsupported"
  | "secret_in_provider_output"
  | "secret_in_stored_bytes"
  | "artifact_missing"
  | "artifact_corrupt"
  | "unmatched_external_op"
  | "branch_busy"
  | "branch_not_runnable";

/** Why a loop stopped before the branch was idle or parked. */
export type Halt = { readonly code: RunErrorCode; readonly message: string };

/** The injected clock. Runners never read wall time; a recorded wait advances it. */
export type Clock = {
  readonly now: () => number;
  readonly sleepUntil: (time: number) => Promise<void>;
};

/** What one dispatch of a tool body established. Anything after dispatch but a result is uncertain. */
export type ToolRun =
  | {
      readonly kind: "done";
      readonly output: string;
      readonly isError: boolean;
      readonly receipt?: string;
      /**
       * The ordered parts the model sees instead of `output` (an image_ref
       * screenshot, citations); `output` is then the plain-text preview for logs and channels.
       */
      readonly content?: readonly ResultPart[];
      /**
       * Model-visible context the result brings, appended with it (recalled memory, retrieved * knowledge: always untrusted reference).
       */
      readonly inject?: readonly z.infer<typeof InjectedData>[];
    }
  | { readonly kind: "unknown"; readonly reason: "timeout" | "transport_error" }
  /** The adapter proves the request never left. */
  | { readonly kind: "not_sent" };

export type ToolContext = {
  readonly effectKey: string;
  readonly callId: string;
  /** The fencing pair a gateway re-checks before an external operation. */
  readonly branchId: string;
  readonly epoch: number;
  readonly principal: Principal;
  readonly signal: AbortSignal;
  /**
   * Re-checks the lease at the tool's real send point, for a tool whose body
   * reaches a remote service: run the transport inside `within(ctx, ...)`.
   */
  readonly fence: () => Promise<Result<void, Stale>>;
};

/** A dispatchable tool: its pinned spec, its body, and the recovery contract its class needs. */
export type ToolImpl = {
  readonly spec: ToolSpec;
  /** The tool's own schema: arguments are parsed with it before anything is authorized. */
  readonly input: z.ZodType;
  readonly run: (
    input: EventOf<"tool_call">["data"]["input"],
    ctx: ToolContext,
  ) => Promise<ToolRun>;
  /** reconcilable: the adapter's lookup by effect key, and whether its not_found is final. */
  readonly reconcile?: {
    /** `input` is the call's recorded input, for a lookup keyed by what was asked. */
    readonly lookup: (
      effectKey: string,
      input: EventOf<"tool_call">["data"]["input"],
    ) => Promise<LookupResult<string>>;
    readonly finality: "final" | "nonfinal";
  };
  /** sandbox_local: kill the call's process group and confirm it is gone. */
  readonly terminate?: (
    effectKey: string,
  ) => Promise<"terminated" | "already_exited" | "unknown">;
  /** idempotent: the provider's clock; absent means the host clock with a doubled skew margin. */
  readonly providerNow?: () => number;
  /** An app tool declared `concurrent: true`: it may run in a group (loop/groups.ts). */
  readonly concurrent?: true;
};

/** A permission_decision the loop records for a new call (fold, hooks later). */
export type Authorization = {
  readonly decision: "allow" | "deny" | "ask";
  readonly source: EventOf<"permission_decision">["data"]["source"];
  readonly rule_id?: string;
  readonly reason?: string;
};

/**
 * Stub mode: every mediated external operation is answered from recorded stubs by
 * (tool, args_hash, occurrence). `undefined` is an unmatched operation: it fails closed.
 */
export type StubGateway = {
  readonly answer: (
    tool: string,
    argsHash: string,
  ) => { readonly output: string; readonly isError: boolean } | undefined;
};

export type LoopConfig = {
  /** The adapter for a settings epoch's model; the loop never sends to any other. */
  readonly models: (ref: Model["info"]["model"]) => Model | undefined;
  readonly tools: ReadonlyMap<string, ToolImpl>;
  /** Decides a call by the spec it was made under (the call-time spec, never a later set's). */
  readonly authorize: (
    call: EventOf<"tool_call">,
    fold: Fold,
    spec: ToolSpec,
  ) => Authorization;
  readonly clock: Clock;
  readonly principal: Principal;
  /** the adapter's declared clock skew margin. */
  readonly skewMarginMs: number;
  readonly stub?: StubGateway;
  /** The agent's output schema, when policy.output is pinned. */
  readonly output?: z.ZodType;
  readonly signal?: AbortSignal;
  /** Transient stream items (text deltas, retry waits); never logged. */
  readonly onDelta?: (requestEventId: string, text: string) => void;
  readonly onEvent?: (event: KnownEvent) => void;
  /** Extensions whose hooks gate, feed and observe the loop, in declaration order. */
  readonly extensions?: readonly LoopExtension[];
  /**
   * L3 restore: reads a file the dropped range touched, as a framework
   * read_only operation. Absent when there is no sandbox: no file is restored.
   */
  readonly readFile?: (path: string) => Promise<Uint8Array | undefined>;
  /** Subagents and the team; absent: spawn_agent and team tools have no agents. */
  readonly agents?: Agents;
  /** A team's lead or member (agent({team})); absent for any other thread. */
  readonly team?: TeamRuntime;
  /** The tree-wide budget ledger; absent: no cost, token or request budget. */
  readonly budgets?: {
    readonly ledger: BudgetLedger;
    /** The ancestors' budgets that cover this thread too. */
    readonly inherited: readonly Covering[];
  };
};

/** A budget covering a thread, as the ledger names it. */
export type Covering = {
  readonly budgetId: string;
  readonly budget: NonNullable<Policy["budget"]>;
  /** An ancestor's budget names its thread; the thread's own and its run's don't. */
  readonly owner?: ThreadId;
  readonly scope: "thread" | "run" | "ancestor";
};

/** The terminal result of a child thread (agent_finished, F7.5). */
export type ChildDone = {
  readonly status: EventOf<"agent_finished">["data"]["status"];
  /** The final text, or the accepted structured value as canonical JSON. */
  readonly output: string;
  readonly usage: Usage;
};

/** How a child run ended: terminal, or parked, which parks its parent instead of finishing. */
export type ChildEnd =
  | ChildDone
  | {
      readonly status: "parked";
      readonly reason: EventOf<"parked">["data"]["reason"];
    };

/** What a parent hands one child run. */
export type ChildRun = {
  readonly threadId: ThreadId;
  readonly parent: {
    readonly thread_id: ThreadId;
    readonly branch_id: BranchId;
    readonly event_id: EventId;
  };
  /** The prompt, then each subagent_stop continue reason, in order. */
  readonly inputs: readonly string[];
  /**
   * The parent's decision for a child's call, under the parent's policy and mode: the child only
   * narrows it. The spec is the child's call-time spec; the parent's fold has no such call.
   */
  readonly ceiling: (
    call: EventOf<"tool_call">,
    spec: ToolSpec,
  ) => Authorization;
  /** The parent's pinned tool names: the child's tools are within them. */
  readonly tools: ReadonlySet<string>;
  /** The parent's team, which the child joins as a member. */
  readonly team: Team;
  /** Every budget covering the parent, which covers the child too. */
  readonly covering: readonly Covering[];
  /**
   * The parent is cancelled, by this principal: the child gets a barrier and no new input, and
   * one that never started is not created (spec/schema/README.md, Subagent cancellation).
   */
  readonly cancel?: Principal;
};

export type Subagent = {
  readonly budget?: NonNullable<Policy["budget"]>;
  /**
   * Runs the child thread (created on first use) until it ends; resumes it after a crash. A
   * child that can't run now (its lease is held elsewhere) is a halt: nothing is recorded.
   */
  readonly run: (child: ChildRun) => Promise<ChildEnd | Halt>;
  /**
   * Stops a running child: a durable cancel_requested{scope: tree} with this reason in its own
   * log (and its descendants'), which it obeys at its next step. True once the child has its
   * barrier; a child with no thread yet gets nothing and is tried again.
   */
  readonly stop: (
    child: ThreadId,
    principal: Principal,
    reason: string,
  ) => Promise<boolean>;
  /** Whether a run of this child in this process still holds its lease: one to adopt. */
  readonly held: (child: ThreadId) => Promise<boolean>;
};

/** Team state lives in the lead's log; members change it only through the lead's writer. */
export type Team = {
  /** Applies a member's team tool call in one append to the lead's log. */
  readonly act: (
    member: string,
    call: EventOf<"tool_call">["data"],
  ) => { readonly isError: boolean; readonly output: string };
  /** Messages to `member` (or to every member) from anyone else, oldest first. */
  readonly inbox: (
    member: string,
  ) => readonly EventOf<"team_message">["data"][];
};

export type Agents = {
  /** This agent's name: its member id in its own team and its parent's. */
  readonly name: string;
  readonly subagent: (name: string) => Subagent | undefined;
  /** The names spawn_agent may start, in declaration order: with this agent, its team. */
  readonly subagents: readonly string[];
  /** Present when this thread is a member: its parent's team. */
  readonly team?: Team;
};

/** An agent a lead's team lists, pinned as a member (spec/schema/README.md, "Teams"). */
export type TeamAgentPin = {
  readonly configHash: string;
  /** The canonical config config_hash names, stored before the start that pins it. */
  readonly config: string;
  /** What one request of it reserves: its model, settings and pinned policy (start's headroom). */
  readonly model: EventOf<"thread_started">["data"]["model"];
  readonly params: EventOf<"thread_started">["data"]["model_params"];
  readonly policy: Policy | undefined;
  /** Its own budget: it covers the member. */
  readonly budget: NonNullable<Policy["budget"]> | undefined;
  /** Its pinned tool names, in order: a dynamic agent's are all a start may choose from, and F. */
  readonly tools: readonly string[];
  /** A dynamic agent: its model keys, the first the default. */
  readonly models?: readonly string[];
};

/** What the loop of a team's lead or member needs from its team. */
export type TeamRuntime = {
  /**
   * The agents start may name, pinned on first use; undefined for an agent the team lacks. With
   * a choice, a dynamic agent's member as that choice defines it (throws ConfigError).
   */
  readonly pin: (
    agent: string,
    choice?: DynamicChoice,
  ) => Promise<TeamAgentPin | undefined>;
  readonly limits: { readonly concurrent: number; readonly mailbox: number };
  /**
   * A member's turn is under the run budget of its request: the root request the turn's opener
   * belongs to (its receipt's provenance, or its task's). Absent for a lead, whose run is its own.
   */
  readonly runCovering?: (opener: KnownEvent) => Covering | undefined;
  /**
   * A member's: the one principal its run acts under. Its consume takes only mail sent under it;
   * the worker runs the member again under the principal of the mail left pending.
   */
  readonly principal?: Principal;
  /** Called after each append of a team thread: the team worker looks for work. */
  readonly notify: () => void;
  /**
   * The lead of an in-process run waits here for its members' progress until its run ends;
   * absent, a thread takes its pending mail and stops once idle (a member run by the worker).
   */
  readonly progress?: () => Promise<void>;
  /** The worker is running a member now: a lead parked on its members waits for it. */
  readonly busy?: () => boolean;
  /** The event ids of team appends; tests inject deterministic ones. */
  readonly mint?: (seq: number, now: number) => string;
};

/** A batch the cancel barrier refused: nothing of the work it would start was recorded. */
export const BARRED: unique symbol = Symbol("barred");
export type Barred = typeof BARRED;
