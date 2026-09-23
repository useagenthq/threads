import type { z } from "zod";
import type { EventOf, Fold } from "../fold/state";
import type { LoopExtension } from "../hooks/types";
import type {
  BranchId,
  EventId,
  KnownEvent,
  Policy,
  Principal,
  ThreadId,
  ToolSpec,
  Usage,
} from "../log";
import type { LookupResult, Model } from "../model";

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
    readonly lookup: (effectKey: string) => Promise<LookupResult<string>>;
    readonly finality: "final" | "nonfinal";
  };
  /** sandbox_local: kill the call's process group and confirm it is gone. */
  readonly terminate?: (
    effectKey: string,
  ) => Promise<"terminated" | "already_exited" | "unknown">;
  /** idempotent: the provider's clock; absent means the host clock with a doubled skew margin. */
  readonly providerNow?: () => number;
};

/** A permission_decision the loop records for a new call. */
export type Authorization = {
  readonly decision: "allow" | "deny" | "ask";
  readonly source: EventOf<"permission_decision">["data"]["source"];
  readonly rule_id?: string;
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
  readonly authorize: (call: EventOf<"tool_call">, fold: Fold) => Authorization;
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
  /** Scrubs host secrets from tool output before it is recorded (C5). */
  readonly redact?: (text: string) => string;
  /** Subagents and the team; absent: spawn_agent and team tools have no agents. */
  readonly agents?: Agents;
};

/** The terminal result of a child thread (agent_finished, F7.5). */
export type ChildEnd = {
  readonly status: EventOf<"agent_finished">["data"]["status"];
  /** The final text, or the accepted structured value as canonical JSON. */
  readonly output: string;
  readonly usage: Usage;
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
  /** The parent's decision for a child's call: the child only narrows it. */
  readonly ceiling: (call: EventOf<"tool_call">) => Authorization;
  /** The parent's pinned tool names: the child's tools are within them. */
  readonly tools: ReadonlySet<string>;
  /** The parent's team, which the child joins as a member. */
  readonly team: Team;
};

export type Subagent = {
  readonly budget?: NonNullable<Policy["budget"]>;
  /** Runs the child thread (created on first use) until it ends; resumes it after a crash. */
  readonly run: (child: ChildRun) => Promise<ChildEnd>;
};

/** Team state lives in the lead's log; members change it only through the lead's writer. */
export type Team = {
  /** Applies a member's team tool call in one append to the lead's log. */
  readonly act: (
    member: string,
    call: EventOf<"tool_call">,
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
  /** Present when this thread is a member: its parent's team. */
  readonly team?: Team;
};
