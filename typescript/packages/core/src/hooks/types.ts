import type { z } from "zod";
import type { EventOf } from "../fold/state";
import type { KnownEvent, ModelSettings, Span } from "../log";
import type { RunErrorCode } from "../loop/types";
import type { ReducedState } from "../reduce";

// The hook set at the loop's level: wire names, what each hook is given,
// and what it may decide. Every decision name is a value of the wire hook_decision.decision
// enum (spec/api.json choice hook-decision-names). The agent layer binds the public camelCase
// hooks (extension()) to these.

export type HookName = EventOf<"hook_decision">["data"]["hook"];

type Call = EventOf<"tool_call">["data"];
type Result = EventOf<"tool_result">["data"];
type Response = EventOf<"model_response" | "model_response_recovered">["data"];

export type SessionSource = "startup" | "resume" | "fork" | "compact";

/** What each hook is called with, besides its context. */
export type HookArgs = {
  readonly session_start: readonly [source: SessionSource];
  readonly session_end: readonly [];
  readonly before_input: readonly [input: EventOf<"user_input">["data"]];
  readonly before_model: readonly [state: ReducedState];
  readonly after_model: readonly [state: ReducedState, response: Response];
  readonly before_tool: readonly [call: Call];
  readonly permission_request: readonly [call: Call];
  readonly permission_denied: readonly [call: Call];
  readonly after_tool: readonly [call: Call, result: Result];
  readonly before_tool_result: readonly [call: Call, result: Result];
  readonly after_tool_batch: readonly [state: ReducedState];
  readonly before_compact: readonly [state: ReducedState];
  readonly after_compact: readonly [state: ReducedState];
  readonly on_stop: readonly [state: ReducedState];
  readonly stop_failure: readonly [code: RunErrorCode];
  readonly subagent_start: readonly [call: Call];
  readonly subagent_stop: readonly [
    finished: EventOf<"agent_finished">["data"],
  ];
  readonly before_model_switch: readonly [
    settings: z.infer<typeof ModelSettings>,
  ];
  readonly after_model_switch: readonly [
    settings: z.infer<typeof ModelSettings>,
  ];
  readonly notification: readonly [event: KnownEvent];
};

type Deny = { readonly decision: "deny"; readonly reason: string };
type Injections = { readonly injections?: readonly string[] };

export type AllowOrDeny = { readonly decision: "allow" } | Deny;
export type StopOrContinue =
  | { readonly decision: "stop" }
  | { readonly decision: "continue"; readonly reason: string };
export type ToolDecision =
  | { readonly decision: "allow" }
  | Deny
  | { readonly decision: "ask"; readonly rule?: string };

/** What each hook returns (spec/api.json types.Hooks). Observation hooks return nothing. */
export type HookReturns = {
  readonly session_start: readonly string[];
  readonly session_end: undefined;
  readonly before_input: ({ readonly decision: "allow" } & Injections) | Deny;
  readonly before_model: ({ readonly decision: "proceed" } & Injections) | Deny;
  readonly after_model:
    | { readonly decision: "proceed" }
    | Deny
    | { readonly decision: "guide"; readonly text: string }
    | { readonly decision: "retry"; readonly reason: string };
  readonly before_tool: ToolDecision;
  readonly permission_request: ToolDecision;
  readonly permission_denied: undefined;
  readonly after_tool: readonly string[];
  readonly before_tool_result:
    | { readonly decision: "proceed" }
    | {
        readonly decision: "redact";
        readonly spans: readonly z.infer<typeof Span>[];
      }
    | Deny;
  readonly after_tool_batch: readonly string[];
  readonly before_compact:
    | { readonly decision: "proceed" }
    | Deny
    | { readonly decision: "guide"; readonly text: string };
  readonly after_compact: readonly string[];
  readonly on_stop: StopOrContinue;
  readonly stop_failure: undefined;
  readonly subagent_start: AllowOrDeny;
  readonly subagent_stop: StopOrContinue;
  readonly before_model_switch: AllowOrDeny;
  readonly after_model_switch: undefined;
  readonly notification: undefined;
};

/** What the loop passes every hook: the call it is about, if any, and the timeout's signal. */
export type HookContext = {
  readonly callId?: string;
  readonly signal: AbortSignal;
};

/** One extension's hooks, bound to its run context; `unknown` until checked at the boundary. */
export type LoopHooks = {
  readonly [K in HookName]?: (
    args: HookArgs[K],
    ctx: HookContext,
  ) => Promise<unknown>;
};

export type LoopExtension = {
  readonly name: string;
  /** default 5 s. */
  readonly timeoutMs: number;
  readonly hooks: LoopHooks;
};
