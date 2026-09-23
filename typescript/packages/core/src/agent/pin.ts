import { z } from "zod";
import { sha256Hex } from "../hash";
import {
  type Budget,
  type ContextPolicy,
  canonicalize,
  type PermissionsPolicy,
  type Policy,
  type RetryPolicy,
  type ToolSpec,
} from "../log";
import { FINAL_OUTPUT, RETRY_DEFAULTS } from "../loop";
import { CONTEXT_DEFAULTS } from "../loop/policy";
import type { KnowledgeProvider, MemoryProvider } from "../memory/protocol";
import { knowledgeSpecs, memorySpecs } from "../memory/tools";
import type { Model } from "../model";
import { DEFAULT_PERMISSIONS } from "../permissions";
import type { Sandbox } from "../sandbox";
import type { EventDraft } from "../store";
import { builtins, type Capabilities, type Egress } from "../tools";
import { frameworkSpec } from "../tools/framework";
import { requireCapabilities } from "../tools/gated";
import { unchecked } from "../validate/json-schema";
import { checkEnforceable } from "./enforceable";
import { ConfigError } from "./errors";
import { type Extension, hookNames } from "./extension";
import {
  checkSkills,
  type Skill,
  skillListing,
  skillPins,
  skillSpecs,
} from "./skills";
import { jsonSchema, type Tool } from "./tool";

// The resolved, secret-free config an agent pins in thread_started: line 0's
// system, tools, model and adapter, plus the policy, hashed as config_hash.

export type PinOptions = {
  readonly name: string;
  readonly model: Model;
  readonly instructions: string;
  readonly tools: readonly Tool<unknown, unknown, never>[];
  readonly output: z.ZodType | undefined;
  readonly outputRetries: number;
  readonly fallback: readonly Model[];
  readonly permissions: Partial<z.infer<typeof PermissionsPolicy>>;
  readonly budget: z.infer<typeof Budget> | undefined;
  readonly retry: Partial<z.infer<typeof RetryPolicy>>;
  readonly context: Partial<z.infer<typeof ContextPolicy>>;
  readonly sandbox: Sandbox | undefined;
  readonly egress: Egress | undefined;
  /** The capability-gated built-ins: web, git, computer, lsp. */
  readonly capabilities: Capabilities;
  readonly extensions: readonly Extension<never>[];
  /** Agent names spawn_agent may start. */
  readonly subagents: readonly string[];
  /** Agent names handoff may target, pinned as policy.handoffs. */
  readonly handoffs: readonly string[];
  /** The MCP servers' tools, resolved at setup. */
  readonly mcp: readonly Tool<unknown, unknown, unknown>[];
  readonly memory: MemoryProvider | undefined;
  readonly memoryWrite: MemoryWrite;
  readonly knowledge: KnowledgeProvider | undefined;
  /** From the host store, listed in line 0 and pinned by config_hash. */
  readonly skills: readonly Skill[];
};

type ThreadStarted = Extract<EventDraft, { type: "thread_started" }>["data"];

/**
 * A thread started by another: its parent link. A subagent also has its parent's tools: it gets
 * no sandbox (isolation none) and team member tools, and never more than those tools.
 */
export type ChildPin = {
  readonly parent: NonNullable<ThreadStarted["parent"]>;
  readonly tools?: ReadonlySet<string>;
};

/** spec/api.json agent memory_write. */
export type MemoryWrite = "deny" | "ask" | "allow_principal" | "allow";

const byName = (a: { readonly name: string }, b: { readonly name: string }) =>
  a.name < b.name ? -1 : a.name > b.name ? 1 : 0;

/** The pinned tool specs and the thread_started draft. Throws ConfigError on a bad setup. */
export function pin(
  options: PinOptions,
  child?: ChildPin,
): {
  readonly specs: readonly ToolSpec[];
  readonly started: EventDraft;
} {
  const within = child?.tools;
  const o = within === undefined ? options : { ...options, sandbox: undefined };
  // A child runs without a sandbox and within its parent's tools, which were checked already.
  if (within === undefined) requireCapabilities(o.capabilities, o.sandbox);
  checkSkills(o.skills);
  checkRetries(o.outputRetries);
  // Built-ins (the framework and provider tools among them) sorted by name, then app tools,
  // then extension and MCP tools sorted by namespaced name.
  const all = [
    ...[
      ...builtins(o.sandbox, o.egress, o.capabilities).map((b) => b.spec),
      ...agentTools(o, within !== undefined).map(frameworkSpec),
      ...(o.memory === undefined ? [] : memorySpecs(o.memory)),
      ...(o.knowledge === undefined ? [] : knowledgeSpecs()),
      ...skillSpecs(o.skills),
    ].toSorted(byName),
    ...o.tools.map((t) => t.spec()),
    ...extensionTools(o.extensions, o.mcp).map((t) => t.spec()),
  ];
  // A child never gains a tool its parent lacks.
  const specs = [
    ...all.filter((t) => within === undefined || within.has(t.name)),
    ...finalOutput(o.output),
  ];
  const exts = o.extensions.map((e) => e.name);
  const again = exts.find((n, i) => exts.indexOf(n) !== i);
  if (again !== undefined)
    throw new ConfigError(
      "duplicate_name",
      `two extensions are named ${again}`,
    );
  const names = specs.map((s) => s.name);
  const twice = names.find((n, i) => names.indexOf(n) !== i);
  if (twice !== undefined)
    throw new ConfigError("duplicate_name", `two tools are named ${twice}`);
  checkEnforceable(o.budget, [o.model, ...o.fallback]);
  const { model, params, adapter } = o.model.info;
  const cfg = {
    agent_name: o.name,
    instructions: instructions(o),
    model,
    model_params: params,
    adapter,
    tools: specs,
    policy: policy(o),
    ...(o.sandbox === undefined
      ? {}
      : { sandbox_provider: o.sandbox.info.provider }),
  };
  const text = canonicalize(z.json().parse({ ...cfg, ...hashedOnly(o) }));
  if (!text.ok) throw new ConfigError("invalid_config", text.error.message);
  return {
    specs,
    started: {
      type: "thread_started",
      type_version: 1,
      critical: true,
      actor: { kind: "host" },
      data: {
        ...cfg,
        config_hash: sha256Hex(text.value),
        ...(child === undefined ? {} : { parent: child.parent }),
      },
    },
  };
}

/**
 * Pinned by config_hash but not model-visible, so not in thread_started's fields: what the
 * extensions hook and observe (hooks load only from the pinned config, and a
 * changed hook set is a new thread), and the sandbox settings a resumed run must match.
 */
function hashedOnly(o: PinOptions): Record<string, unknown> {
  return {
    ...(o.memory === undefined ? {} : { memory_write: o.memoryWrite }),
    ...(o.skills.length === 0 ? {} : { skills: skillPins(o.skills) }),
    ...(o.extensions.length === 0
      ? {}
      : {
          extensions: o.extensions.map((e) => ({
            name: e.name,
            hooks: hookNames(e),
            observers: Object.keys(e.on ?? {}).toSorted(),
            hook_timeout_ms: e.hookTimeoutMs ?? null,
          })),
        }),
    ...(o.sandbox === undefined
      ? {}
      : {
          sandbox: {
            provider: o.sandbox.info.provider,
            egress: o.sandbox.info.egress,
            capture_classes: [...o.sandbox.info.capture_classes],
            policy: o.egress ?? null,
          },
        }),
  };
}

/**
 * the base instructions, then each extension's, in declaration order, then the
 * skill listing, then the agents spawn_agent and handoff may name.
 */
function instructions(o: PinOptions): string {
  const listed = (label: string, names: readonly string[]): string[] =>
    names.length === 0 ? [] : [`${label}: ${names.join(", ")}.`];
  return [
    o.instructions,
    ...o.extensions.flatMap((e) => e.instructions ?? []),
    ...skillListing(o.skills),
    ...listed("Subagents you can start with spawn_agent", o.subagents),
    ...listed("Agents you can hand the conversation to", o.handoffs),
  ]
    .filter((t) => t !== "")
    .join("\n\n");
}

const TEAM = [
  "send_message",
  "team_task_claim",
  "team_task_create",
  "team_task_update",
];

/** todo_write always; spawn and team tools with agents; handoff with targets. */
function agentTools(o: PinOptions, member: boolean): readonly string[] {
  return [
    "todo_write",
    ...(o.subagents.length > 0 ? ["spawn_agent"] : []),
    ...(o.subagents.length > 0 || member ? TEAM : []),
    ...(o.handoffs.length > 0 ? ["handoff"] : []),
  ];
}

/**
 * Extension tools, namespaced <ext>__<tool>, with the MCP servers' mcp__<server>__<tool>,
 * sorted by that name.
 */
export function extensionTools<Deps>(
  extensions: readonly Extension<Deps>[],
  mcp: readonly Tool<unknown, unknown, unknown>[],
): readonly Tool<unknown, unknown, Deps>[] {
  return [
    ...extensions.flatMap((e) =>
      (e.tools ?? []).map((t) => namespaced(t, e.name)),
    ),
    ...mcp,
  ].toSorted(byName);
}

function namespaced<Deps>(
  t: Tool<unknown, unknown, Deps>,
  ext: string,
): Tool<unknown, unknown, Deps> {
  const name = `${ext}__${t.name}`;
  return {
    name,
    spec: () => ({ ...t.spec(), name }),
    bind: (env) => {
      const impl = t.bind(env);
      return { ...impl, spec: { ...impl.spec, name } };
    },
  };
}

/** Tool mode: final_output takes the output schema and ends the turn. */
function finalOutput(output: z.ZodType | undefined): readonly ToolSpec[] {
  if (output === undefined) return [];
  return [
    {
      name: FINAL_OUTPUT,
      description: "Return the final structured result.",
      input_schema: jsonSchema(FINAL_OUTPUT, output),
      effect_class: "read_only",
      ends_turn: true,
    },
  ];
}

function policy(o: PinOptions): Policy {
  const models = [o.model, ...o.fallback].map((m) => m.info.limits);
  return {
    models: models.filter(
      (m, i) =>
        models.findIndex(
          (x) => x.provider === m.provider && x.name === m.name,
        ) === i,
    ),
    // Prices are nano-USD (spec/schema/README.md); without a currency cost() would be null.
    ...(models.some((m) => m.price !== undefined) ? { currency: "USD" } : {}),
    permissions: { ...DEFAULT_PERMISSIONS, ...o.permissions },
    retry: { ...RETRY_DEFAULTS, ...o.retry },
    context: { ...CONTEXT_DEFAULTS, ...o.context },
    ...(o.fallback.length === 0
      ? {}
      : {
          fallback: o.fallback.map((m) => ({
            model: m.info.model,
            model_params: m.info.params,
            adapter: m.info.adapter,
            reasoning_carryover: "keep" as const,
          })),
        }),
    ...(o.budget === undefined ? {} : { budget: o.budget }),
    ...(o.handoffs.length === 0 ? {} : { handoffs: [...o.handoffs] }),
    ...(o.output === undefined
      ? {}
      : { output: outputPolicy(o.output, o.outputRetries) }),
  };
}

/** outputRetries counts failed candidates: a non-negative integer. */
function checkRetries(n: number): void {
  if (!Number.isSafeInteger(n) || n < 0)
    throw new ConfigError(
      "invalid_config",
      `outputRetries must be an integer from 0 to 2**53 - 1, got ${n}`,
    );
}

function outputPolicy(
  schema: z.ZodType,
  maxRetries: number,
): NonNullable<Policy["output"]> {
  const exported = jsonSchema("output", schema);
  // Refused here, never by a run that has an answer to record: the log checks every keyword.
  const why = unchecked(exported);
  if (why !== undefined)
    throw new ConfigError(
      "invalid_config",
      `output: ${why}; use a bound, a length, a pattern, an enum or a format the log checks`,
    );
  const text = canonicalize(exported);
  if (!text.ok) throw new ConfigError("invalid_config", text.error.message);
  return {
    schema: exported,
    schema_sha256: sha256Hex(text.value),
    mode: "tool",
    max_retries: maxRetries,
  };
}
