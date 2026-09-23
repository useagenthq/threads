import type { z } from "zod";
import type {
  Budget,
  ContextPolicy,
  InputPart,
  KnownEvent,
  PermissionsPolicy,
  Principal,
  RetryPolicy,
} from "../log";
import type { KnowledgeProvider, MemoryProvider } from "../memory/protocol";
import type { Model } from "../model";
import type { Sandbox } from "../sandbox";
import type { Capabilities, Egress } from "../tools";
import type { GitOptions } from "../tools/git/host";
import { subagent } from "./child";
import { checkTree } from "./enforceable";
import { ConfigError } from "./errors";
import type { Extension } from "./extension";
import { target } from "./handoff";
import { hosted } from "./hosted";
import { type MemoryWrite, type PinOptions, pin } from "./pin";
import { register } from "./registry";
import type { RunResult } from "./result";
import { type Resolved, type RunOptions, run } from "./run";
import { isMcp, type McpServer, once } from "./setup";
import type { Skill } from "./skills";
import type { Tool } from "./tool";

// agent() (spec/api.json): pure. It does no I/O, reads no env and opens no sockets; its setup
// is checked by check() or on the first run.

export type AgentOptions<Deps, Output> = {
  readonly model: Model;
  /** Pinned system text (Render v1 line 0). */
  readonly instructions?: string;
  readonly name?: string;
  /** App tools and MCP servers (mcp() in @threads/mcp). */
  readonly tools?: readonly (Tool<unknown, unknown, Deps> | McpServer)[];
  /** Structured final output; absent: the output is the final text. */
  readonly output?: z.ZodType<Output>;
  readonly outputRetries?: number;
  /** Fallback models in order. */
  readonly fallback?: readonly Model[];
  readonly permissions?: Partial<z.infer<typeof PermissionsPolicy>>;
  /** Thread budget: it covers the thread and every descendant. */
  readonly budget?: z.infer<typeof Budget>;
  /**
   * How a budget counts an attempt with no known usage. "stop" lets a limit the model can't
   * bound per attempt pass setup; the attempt is refused at run time instead. Absent: upper_bound.
   */
  readonly onUnknownUsage?: PinOptions["onUnknownUsage"];
  readonly retry?: Partial<z.infer<typeof RetryPolicy>>;
  readonly context?: Partial<z.infer<typeof ContextPolicy>>;
  /** Absent: no sandbox tools (bash, files). */
  readonly sandbox?: Sandbox;
  /** Sandbox egress: absent is deny-all; "unenforced" opts in to a provider that can't enforce it. */
  readonly egress?: Egress;
  /** Host-side web_fetch (fetch: true) and web_search (a SearchBackend). */
  readonly web?: Capabilities["web"];
  /** The git gateway: git_clone, git_fetch, git_push, open_pull_request. */
  readonly git?: GitOptions;
  /** computer_screenshot and computer; the sandbox must have a desktop. */
  readonly computer?: boolean;
  /** lsp for these languages, run by the sandbox image's servers. */
  readonly lsp?: { readonly languages: readonly string[] };
  /** Instructions, tools, hooks and observers, in this order. */
  readonly extensions?: readonly Extension<Deps>[];
  /** Agents spawn_agent may start, by name. Team tools come with them. */
  readonly subagents?: readonly Agent<never, unknown>[];
  /** Agents this one may hand the conversation to, pinned as policy.handoffs. */
  readonly handoffs?: readonly Agent<never, unknown>[];
  /** Saved cross-run memory: localMemory() or an adapter. */
  readonly memory?: MemoryProvider;
  /** Write authority for save_memory and forget_memory. */
  readonly memoryWrite?: MemoryWrite;
  /** Retrieval over host-admitted sources: localKnowledge({paths}) or an adapter. */
  readonly knowledge?: KnowledgeProvider;
  /** Skills from the host store: listed in line 0, loaded with load_skill. */
  readonly skills?: readonly Skill[];
  /**
   * Who may answer this agent's approval challenges through a host. Absent:
   * the host API's authenticated principals of the thread's tenant, and nobody over a channel.
   */
  readonly approvers?: readonly Principal[];
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
  const tools = (options.tools ?? []).flatMap((t) => (isMcp(t) ? [] : [t]));
  const servers = (options.tools ?? []).filter(isMcp);
  const def: Resolved<Deps, Output> = {
    name: options.name ?? "agent",
    model: options.model,
    instructions: options.instructions ?? "",
    tools,
    bindable: tools,
    output: options.output,
    outputRetries: options.outputRetries ?? 2,
    fallback: options.fallback ?? [],
    permissions: options.permissions ?? {},
    budget: options.budget,
    onUnknownUsage: options.onUnknownUsage,
    retry: options.retry ?? {},
    context: options.context ?? {},
    sandbox: options.sandbox,
    egress: options.egress,
    capabilities: capabilitiesOf(options),
    extensions: options.extensions ?? [],
    hookable: options.extensions ?? [],
    memory: options.memory,
    memoryWrite: options.memoryWrite ?? "ask",
    knowledge: options.knowledge,
    skills: options.skills ?? [],
    setup: once(options.extensions ?? [], servers, [
      options.memory,
      options.knowledge,
    ]),
    decode,
    ...agentsOf(options),
  };
  const handle: Agent<Deps, Output> = {
    name: def.name,
    run: (input, runOptions = {}) => run(def, input, runOptions),
    stream: (input, runOptions = {}) => stream(def, input, runOptions),
    check: async () => {
      try {
        pin({ ...def, mcp: await def.setup() });
        checkTree(def);
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
  register(handle, {
    child: subagent(def),
    target: target(def),
    enforce: (covering) => checkTree(def, covering),
    host: hosted(def, options.approvers),
  });
  return handle;
}

function capabilitiesOf<Deps, Output>(
  o: AgentOptions<Deps, Output>,
): Capabilities {
  return {
    ...(o.web === undefined ? {} : { web: o.web }),
    ...(o.git === undefined ? {} : { git: o.git }),
    ...(o.computer === undefined ? {} : { computer: o.computer }),
    ...(o.lsp === undefined ? {} : { lsp: o.lsp }),
  };
}

/** The agents spawn_agent and handoff may name, and their names as pinned. */
function agentsOf<Deps, Output>(
  options: AgentOptions<Deps, Output>,
): Pick<
  Resolved<Deps, Output>,
  "subagents" | "handoffs" | "agents" | "targets"
> {
  // Copies: a list the caller changes later can't change the pinned agents, or form a cycle.
  const agents = [...(options.subagents ?? [])];
  const targets = [...(options.handoffs ?? [])];
  return {
    subagents: agents.map((a) => a.name),
    handoffs: targets.map((a) => a.name),
    agents,
    targets,
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
