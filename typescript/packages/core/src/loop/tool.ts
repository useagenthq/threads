import type { z } from "zod";
import type { EventOf } from "../fold/state";
import type {
  ArtifactRef,
  InjectedData,
  KnownEvent,
  Principal,
  ResultPart,
  ToolSpec,
} from "../log";
import type { LookupResult, ModelContext } from "../model";
import type { Result } from "../result";
import type { Stale } from "../sandbox/protocol";
import type { EventDraft } from "../store/admit";

// What a dispatchable tool is: the outcome one dispatch establishes, the context it runs in, and
// the recovery contract its effect class needs. Separate from the loop's own config, because an
// adapter package (a tool provider, an A2A remote) builds a ToolImpl and nothing else here.

/** What one dispatch of a tool body established. Anything after dispatch but a result is uncertain. */
export type ToolRun =
  | {
      readonly kind: "done";
      readonly output: string;
      readonly isError: boolean;
      readonly receipt?: string;
      /**
       * What this run observed, appended after the call's `effect_commit` and before its result,
       * so a rule that reads a receipt first (a `remote_task_state` after its commit) holds. The
       * loop never reorders them.
       */
      readonly events?: readonly EventDraft[];
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
  /**
   * The branch's events as this dispatch sees them. A tool that derives what it sends from its
   * own log reads it here (an A2A send reads the card its thread pinned and the `remote_call` of
   * the attempt it is reconciling) instead of keeping process state a crash would lose.
   */
  readonly events: () => readonly KnownEvent[];
  /** Stores bytes before any event names them, and returns their ref. Text is redacted (C5). */
  readonly store: (
    bytes: Uint8Array | string,
    mediaType: string,
  ) => Promise<ArtifactRef>;
  /**
   * An artifact this branch's log names, with its hash and length verified. A re-dispatch reads
   * the bytes its first attempt stored and sends them unchanged, rather than serializing a second
   * body that could differ by one byte and defeat a peer's deduplication.
   */
  readonly read: ModelContext["read"];
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
  /**
   * Events this attempt must make durable in the same append as its `effect_begin`, before any
   * byte leaves: an A2A send's `remote_card` and `remote_call` (semantic rules 56 and 58). An
   * error is a refusal — nothing begins and nothing is sent — and closes the call.
   */
  readonly begin?: (
    input: EventOf<"tool_call">["data"]["input"],
    ctx: ToolContext,
  ) => Promise<Result<readonly EventDraft[], string>>;
  /** reconcilable: the adapter's lookup by effect key, and whether its not_found is final. */
  readonly reconcile?: {
    /** `input` is the call's recorded input, for a lookup keyed by what was asked. */
    readonly lookup: (
      input: EventOf<"tool_call">["data"]["input"],
      ctx: ToolContext,
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
