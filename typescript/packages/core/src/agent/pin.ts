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
import type { KnowledgeProvider, MemoryProvider } from "../memory/protocol";
import { knowledgeSpecs, memorySpecs } from "../memory/tools";
import type { Model } from "../model";
import type { Sandbox } from "../sandbox";
import type { EventDraft } from "../store";
import { TEAM_TOOLS, TEAM_TOOLS_PINNED } from "../team/constants";
import { builtins, type Capabilities, type Egress } from "../tools";
import { frameworkSpec } from "../tools/framework";
import { requireCapabilities } from "../tools/gated";
import { checkEnforceable } from "./enforceable";
import { ConfigError } from "./errors";
import { type Extension, hookNames } from "./extension";
import { checkRetries, checkStyles, finalOutput, policy } from "./policy";
import {
  checkSkills,
  type Skill,
  skillListing,
  skillPins,
  skillSpecs,
} from "./skills";
import type { Tool } from "./tool";

// The resolved, secret-free config an agent pins in thread_started: line 0's
// system, tools, model and adapter, plus the policy, hashed as config_hash.

export type PinOptions = {
  readonly name: string;
  readonly model: Model;
  readonly instructions: string;
  readonly tools: readonly Tool<unknown, unknown, never>[];
  readonly output: z.ZodType | undefined;
  readonly outputRetries: number;
  /** Pinned as policy.output_styles when any is defined. */
  readonly outputStyles: Readonly<Record<string, string>>;
  readonly fallback: readonly Model[];
  readonly permissions: Partial<z.infer<typeof PermissionsPolicy>>;
  readonly budget: z.infer<typeof Budget> | undefined;
  /** Pinned as policy.on_unknown_usage when set; absent: upper_bound. */
  readonly onUnknownUsage: Policy["on_unknown_usage"];
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
  /** agent({team}): the agents start may name. Undefined: no team. */
  readonly team: readonly string[] | undefined;
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

/**
 * The pinned tool specs, the thread_started draft and the canonical config its config_hash names
 * (stored before a team member's start). Throws ConfigError on a bad setup.
 */
export function pin(
  options: PinOptions,
  child?: ChildPin,
  member: boolean = child?.parent.relation === "team_member",
): {
  readonly specs: readonly ToolSpec[];
  readonly started: EventDraft;
  readonly config: string;
} {
  const within = child?.tools;
  const o = within === undefined ? options : { ...options, sandbox: undefined };
  // A child runs without a sandbox and within its parent's tools, which were checked already.
  if (within === undefined) requireCapabilities(o.capabilities, o.sandbox);
  checkSkills(o.skills);
  checkRetries(o.outputRetries);
  checkStyles(o.outputStyles);
  // Built-ins (the framework and provider tools among them) sorted by name, then app tools,
  // then extension and MCP tools sorted by namespaced name.
  const all = [
    ...[
      ...builtins(o.sandbox, o.egress, o.capabilities).map((b) => b.spec),
      ...agentTools(
        o,
        within !== undefined,
        o.team !== undefined || member,
      ).map(frameworkSpec),
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
  if (o.team !== undefined || member) checkTeamNames(o);
  const names = specs.map((s) => s.name);
  const twice = names.find((n, i) => names.indexOf(n) !== i);
  if (twice !== undefined)
    throw new ConfigError("duplicate_name", `two tools are named ${twice}`);
  checkEnforceable(o.budget, [o.model, ...o.fallback], o.onUnknownUsage);
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
  const concurrent = [...o.tools, ...extensionTools(o.extensions, o.mcp)]
    .filter((t) => t.concurrent === true && names.includes(t.name))
    .map((t) => t.name)
    .toSorted();
  const text = canonicalize(
    z.json().parse({
      ...cfg,
      ...hashedOnly(o),
      // Changes when reads run, not what the model sees: hashed, not in line 0.
      ...(concurrent.length === 0 ? {} : { concurrent_tools: concurrent }),
    }),
  );
  if (!text.ok) throw new ConfigError("invalid_config", text.error.message);
  return {
    specs,
    config: text.value,
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
 * skill listing, then the agents spawn_agent, handoff and start may name.
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
    ...listed("Agents you can start as team members with start", o.team ?? []),
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

/**
 * todo_write always; spawn and task-board tools with subagents; handoff with targets; the team
 * tools for a lead (agent({team})) and for a team's members.
 */
function agentTools(
  o: PinOptions,
  subagent: boolean,
  team: boolean,
): readonly string[] {
  return [
    "todo_write",
    ...(o.subagents.length > 0 ? ["spawn_agent"] : []),
    ...(o.subagents.length > 0 || subagent ? TEAM : []),
    ...(o.handoffs.length > 0 ? ["handoff"] : []),
    ...(team ? TEAM_TOOLS_PINNED : []),
  ];
}

/** A team thread's own tools can't take a team tool's name, pinned yet or not. */
function checkTeamNames(o: PinOptions): void {
  const own = [...o.tools, ...extensionTools(o.extensions, o.mcp)].map(
    (t) => t.name,
  );
  const taken = own.find((n) => TEAM_TOOLS.includes(n));
  if (taken !== undefined)
    throw new ConfigError(
      "duplicate_name",
      `tool ${taken}: an agent in a team can't have a tool named ${TEAM_TOOLS.join(", ")}; rename it`,
    );
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
    ...(t.concurrent === undefined ? {} : { concurrent: t.concurrent }),
    spec: () => ({ ...t.spec(), name }),
    bind: (env) => {
      const impl = t.bind(env);
      return { ...impl, spec: { ...impl.spec, name } };
    },
  };
}
