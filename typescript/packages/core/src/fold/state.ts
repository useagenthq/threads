import type {
  BranchId,
  EventId,
  KnownEvent,
  ParsedLine,
  Policy,
  ToolSpec,
} from "../log";

/** The known event with tag `T`. */
export type EventOf<T extends KnownEvent["type"]> = Extract<
  KnownEvent,
  { type: T }
>;
/** An event line of the resolved chain: known, or unknown and non-critical. */
export type EventLine = Extract<
  ParsedLine,
  { kind: "event" } | { kind: "unknown_event" }
>;
export type ParkAddress = EventOf<"parked">["data"]["address"];
export type PermissionMode = EventOf<"mode_changed">["data"]["to"];
export type ModelRef = EventOf<"thread_started">["data"]["model"];
export type Todo = EventOf<"todos_updated">["data"]["todos"][number];
export type EffectStatus = "begun" | "committed" | "unknown" | "resolved";
export type ResultEvent = EventOf<"tool_result"> | EventOf<"tool_result_late">;

export type CallState = {
  readonly branchId: BranchId;
  readonly effectClass: ToolSpec["effect_class"] | undefined;
  /** permission_decision allow, or a consumed matching approval (rule 8). */
  allowed: boolean;
  /** A cancel_requested came after the call (rule 8). */
  barrier: boolean;
  /** The latest recorded result; undefined while pending. */
  result: ResultEvent | undefined;
  deferred: boolean;
  late: boolean;
};

export type TaskState = {
  readonly status: "open" | "claimed" | "completed" | "failed";
  readonly owner?: string;
  readonly blockedBy: readonly string[];
};

export type SnapshotPoint = {
  readonly seq: number;
  readonly eventId: EventId;
  /** The quiescence predicate held when the snapshot was appended (C4). */
  readonly quiescent: boolean;
  readonly expiresAt: number | null;
};

type Accepted = Extract<
  EventOf<"output_validated">["data"],
  { outcome: "accepted" }
>;
export type Output =
  | { readonly outcome: "none" }
  | { readonly outcome: "rejected" }
  | { readonly outcome: "accepted"; readonly value: Accepted["value"] };

/**
 * Everything the semantic rules and reduce read, folded over the resolved chain.
 * A mutable accumulator on purpose: one fold per chain, cloned only to roll back a batch.
 */
export type Fold = {
  seq: number;
  epoch: number;
  readonly eventIds: Set<string>;
  policy: Policy | undefined;
  tools: readonly ToolSpec[];
  model: ModelRef | undefined;
  mode: PermissionMode;
  turnOpen: boolean;
  turns: number;
  readonly calls: Map<string, CallState>;
  /** call_ids with a tool_call and no result yet, in call order. */
  readonly pending: Set<string>;
  readonly effects: Map<string, { callId: string; status: EffectStatus }>;
  readonly parked: ParkAddress[];
  readonly snapshots: SnapshotPoint[];
  readonly usage: { input: number; output: number; unknown: number };
  readonly cancelScopes: Map<string, string>;
  cancelled: boolean;
  repair: boolean;
  /** model_request event_id → whether it is a compaction side request. */
  readonly requests: Map<string, { readonly compaction: boolean }>;
  /** Requests awaiting a response or abandonment. */
  readonly awaiting: Set<string>;
  /** Response text per compaction request, for the summary check (rule 10). */
  readonly summaries: Map<string, string>;
  /** boundaries[seq]: no pending call and no awaited attempt after that event. */
  readonly boundaries: boolean[];
  readonly ranges: (readonly [number, number])[];
  compactionFailures: number;
  readonly approvals: Map<
    string,
    { readonly callId: string; readonly argsHash: string; consumed: boolean }
  >;
  budgetBlocked: boolean;
  handedOff: boolean;
  readonly children: Map<string, string>;
  readonly tasks: Map<string, TaskState>;
  todos: readonly Todo[];
  output: Output;
  readonly messageIds: Set<string>;
  readonly itemKeys: Set<string>;
  readonly occurrenceIds: Set<string>;
};

export function emptyFold(): Fold {
  return {
    seq: 0,
    epoch: 0,
    eventIds: new Set(),
    policy: undefined,
    tools: [],
    model: undefined,
    mode: "default",
    turnOpen: false,
    turns: 0,
    calls: new Map(),
    pending: new Set(),
    effects: new Map(),
    parked: [],
    snapshots: [],
    usage: { input: 0, output: 0, unknown: 0 },
    cancelScopes: new Map(),
    cancelled: false,
    repair: false,
    requests: new Map(),
    awaiting: new Set(),
    summaries: new Map(),
    boundaries: [true],
    ranges: [],
    compactionFailures: 0,
    approvals: new Map(),
    budgetBlocked: false,
    handedOff: false,
    children: new Map(),
    tasks: new Map(),
    todos: [],
    output: { outcome: "none" },
    messageIds: new Set(),
    itemKeys: new Set(),
    occurrenceIds: new Set(),
  };
}

export function sameAddress(a: ParkAddress, b: ParkAddress): boolean {
  return a.kind === b.kind && a.id === b.id;
}

/** The effect key: `<branch_id of the tool_call event>:<call_id>` (wire rule 13). */
export function effectKey(
  fold: Fold,
  callId: string,
  branchId: string,
): string {
  return `${fold.calls.get(callId)?.branchId ?? branchId}:${callId}`;
}

/** The text of a response's text parts, joined. */
export function responseText(
  content: EventOf<"model_response">["data"]["content"],
): string {
  return content
    .map((part) => (part.type === "text" ? part.text : ""))
    .join("");
}
