import type { z } from "zod";
import type {
  Budget,
  ContextPolicy,
  InputPart,
  KnownEvent,
  PermissionsPolicy,
  RetryPolicy,
} from "../log";
import type { Model } from "../model";
import type { Sandbox } from "../sandbox";
import type { Egress } from "../tools";
import { subagent } from "./child";
import { ConfigError } from "./errors";
import type { Extension } from "./extension";
import { pin } from "./pin";
import { register } from "./registry";
import type { RunResult } from "./result";
import { type Resolved, type RunOptions, run } from "./run";
import type { Tool } from "./tool";

// agent() (spec/api.json): pure. It does no I/O, reads no env and opens no sockets; its setup
// is checked by check() or on the first run.

export type AgentOptions<Deps, Output> = {
  readonly model: Model;
  /** Pinned system text (Render v1 line 0). */
  readonly instructions?: string;
  readonly name?: string;
  readonly tools?: readonly Tool<unknown, unknown, Deps>[];
  /** Structured final output; absent: the output is the final text. */
  readonly output?: z.ZodType<Output>;
  readonly outputRetries?: number;
  /** Fallback models in order. */
  readonly fallback?: readonly Model[];
  readonly permissions?: Partial<z.infer<typeof PermissionsPolicy>>;
  /** Thread budget: it covers the thread and every descendant. */
  readonly budget?: z.infer<typeof Budget>;
  readonly retry?: Partial<z.infer<typeof RetryPolicy>>;
  readonly context?: Partial<z.infer<typeof ContextPolicy>>;
  /** Absent: no sandbox tools (bash, files). */
  readonly sandbox?: Sandbox;
  /** Sandbox egress: absent is deny-all; "unenforced" opts in to a provider that can't enforce it. */
  readonly egress?: Egress;
  /** Instructions, tools, hooks and observers, in this order. */
  readonly extensions?: readonly Extension<Deps>[];
  /** Agents spawn_agent may start, by name. Team tools come with them. */
  readonly subagents?: readonly Agent<never, unknown>[];
};

/** One item of stream(): a committed event, or a transient text delta (never logged). */
export type StreamEvent =
  | { readonly kind: "event"; readonly event: KnownEvent }
  | {
      readonly kind: "delta";
      readonly request_event_id: string;
      readonly text: string;
    };

/** stream(): a subscription to the run's log, plus its result. Not a second loop. */
export type RunStream<Output = string> = AsyncIterable<StreamEvent> & {
  readonly result: Promise<RunResult<Output>>;
};

export type RunInput = string | readonly InputPart[];

export type Agent<Deps = undefined, Output = string> = {
  readonly name: string;
  readonly run: (
    input: RunInput,
    options?: RunOptions<Deps>,
  ) => Promise<RunResult<Output>>;
  readonly stream: (
    input: RunInput,
    options?: RunOptions<Deps>,
  ) => RunStream<Output>;
  readonly check: () => Promise<
    | { readonly ok: true; readonly value: undefined }
    | {
        readonly ok: false;
        readonly error: {
          readonly code: ConfigError["code"];
          readonly message: string;
        };
      }
  >;
};

export function agent<Deps = undefined>(
  options: AgentOptions<Deps, string> & { readonly output?: undefined },
): Agent<Deps, string>;
export function agent<Deps, Output>(
  options: AgentOptions<Deps, Output> & { readonly output: z.ZodType<Output> },
): Agent<Deps, Output>;
export function agent<Deps, Output>(
  options: AgentOptions<Deps, Output>,
): Agent<Deps, Output> | Agent<Deps, string> {
  const { output: schema, ...rest } = options;
  if (schema === undefined) return build<Deps, string>(rest, (text) => text);
  return build(options, (_text, accepted) => schema.parse(accepted));
}

function build<Deps, Output>(
  options: AgentOptions<Deps, Output>,
  decode: Resolved<Deps, Output>["decode"],
): Agent<Deps, Output> {
  const def: Resolved<Deps, Output> = {
    name: options.name ?? "agent",
    model: options.model,
    instructions: options.instructions ?? "",
    tools: options.tools ?? [],
    bindable: options.tools ?? [],
    output: options.output,
    outputRetries: options.outputRetries ?? 2,
    fallback: options.fallback ?? [],
    permissions: options.permissions ?? {},
    budget: options.budget,
    retry: options.retry ?? {},
    context: options.context ?? {},
    sandbox: options.sandbox,
    egress: options.egress,
    extensions: options.extensions ?? [],
    hookable: options.extensions ?? [],
    setup: once(options.extensions ?? []),
    decode,
    subagents: (options.subagents ?? []).map((a) => a.name),
    handoffs: [],
    agents: options.subagents ?? [],
  };
  const handle: Agent<Deps, Output> = {
    name: def.name,
    run: (input, runOptions = {}) => run(def, input, runOptions),
    stream: (input, runOptions = {}) => stream(def, input, runOptions),
    check: async () => {
      try {
        pin(def);
        await def.setup();
        return { ok: true, value: undefined };
      } catch (error) {
        if (!(error instanceof ConfigError)) throw error;
        return {
          ok: false,
          error: { code: error.code, message: error.message },
        };
      }
    },
  };
  register(handle, subagent(def));
  return handle;
}

/** Each extension's setup runs once, at check() or the first run; a throw is a ConfigError. */
function once<Deps>(
  extensions: readonly Extension<Deps>[],
): () => Promise<void> {
  let done: Promise<void> | undefined;
  const all = async (): Promise<void> => {
    for (const e of extensions) {
      try {
        await e.setup?.();
      } catch (error) {
        throw new ConfigError(
          "invalid_config",
          `extension ${e.name}: setup failed: ${String(error)}`,
        );
      }
    }
  };
  return () => {
    done ??= all();
    return done;
  };
}

function stream<Deps, Output>(
  def: Resolved<Deps, Output>,
  input: RunInput,
  options: RunOptions<Deps>,
): RunStream<Output> {
  const queue: StreamEvent[] = [];
  let wake: (() => void) | undefined;
  let done = false;
  const push = (item: StreamEvent): void => {
    queue.push(item);
    wake?.();
  };
  const result = run(def, input, options, {
    onEvent: (event) => push({ kind: "event", event }),
    onDelta: (id, text) => push({ kind: "delta", request_event_id: id, text }),
  });
  const finished = async (): Promise<void> => {
    try {
      await result;
    } catch {
      // The caller sees the failure through `result`; the iterator just ends.
    } finally {
      done = true;
      wake?.();
    }
  };
  void finished();
  return {
    result,
    async *[Symbol.asyncIterator]() {
      for (;;) {
        const next = queue.shift();
        if (next !== undefined) yield next;
        else if (done) return;
        else {
          const { promise, resolve } = Promise.withResolvers<void>();
          wake = resolve;
          await promise;
        }
      }
    },
  };
}
