import { describe, expect, test } from "bun:test";
import {
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { z } from "zod";
import { type Agent, agent } from "../../src/agent/agent";
import type { DeferTools } from "../../src/agent/defer";
import { type Extension, extension } from "../../src/agent/extension";
import { hostRunner, memberEntry } from "../../src/agent/registry";
import { dynamicAgent } from "../../src/agent/team/dynamic";
import type { DynamicAgent } from "../../src/agent/team/types";
import {
  Budget,
  ContextPolicy,
  MemberDefine,
  PermissionsPolicy,
  RetryPolicy,
} from "../../src/log";
import { RETRY_DEFAULTS } from "../../src/loop";
import { CONTEXT_DEFAULTS } from "../../src/loop/policy";
import { localMemory } from "../../src/memory/local-memory";
import { type Model, scriptedModel } from "../../src/model";
import { markTestKit } from "../../src/model/guard";
import { DEFAULT_PERMISSIONS } from "../../src/permissions";
import { fakeSandbox } from "../../src/sandbox/fake";
import type { Workspace } from "../../src/workspace/resolve";

// The same agent pins the same thread_started and config_hash in both languages
// (spec/schema/README.md, "The pinned config"): the shared vector pins the bytes.

// Each given section is the default one with the vector's fields replaced, as Python takes it;
// the fields alone override the same defaults (policy.ts).
const Fields = z.record(z.string(), z.json()).optional();
const section = <T>(schema: z.ZodType<T>, defaults: T) =>
  Fields.transform((f) =>
    f === undefined ? undefined : schema.parse({ ...defaults, ...f }),
  );
const ModelDef = z.strictObject({
  name: z.string(),
  price: z
    .strictObject({ input: z.number().int(), output: z.number().int() })
    .optional(),
  cache_ttl_ms: z.number().int().optional(),
  /** A dynamic agent's model key. */
  key: z.string().optional(),
});
const ExtensionDef = z.strictObject({
  name: z.string(),
  instructions: z.string().optional(),
  hooks: z.array(z.enum(["session_end", "notification"])).optional(),
  observers: z.array(z.string()).optional(),
  hook_timeout_ms: z.number().int().optional(),
});
/** agent({workspace}); the vector spells its keys as the wire does. */
const WorkspaceDef = z.strictObject({
  files: z.record(z.string(), z.string()).optional(),
  local_dir: z.string().optional(),
  include: z.array(z.string()).optional(),
});
/** One entry of a case's `directory`, written to disk before the agent resolves it. */
const DirEntry = z.strictObject({
  path: z.string(),
  text: z.string().optional(),
  exec: z.boolean().optional(),
  symlink: z.string().optional(),
  dir: z.boolean().optional(),
});

const Plain = z.strictObject({
  name: z.string(),
  instructions: z.string(),
  model: ModelDef.optional(),
  fallback: z.array(ModelDef).optional(),
  permissions: section(PermissionsPolicy, DEFAULT_PERMISSIONS),
  retry: section(RetryPolicy, RETRY_DEFAULTS),
  context: section(ContextPolicy, CONTEXT_DEFAULTS),
  budget: Budget.optional(),
  on_unknown_usage: z.enum(["upper_bound", "stop"]).optional(),
  output_styles: z.record(z.string(), z.string()).optional(),
  extensions: z.array(ExtensionDef).optional(),
  sandbox: z.literal("fake").optional(),
  workspace: WorkspaceDef.optional(),
  egress: z.literal("unenforced").optional(),
  /** A dynamic agent: its models by key, in order. */
  models: z.array(ModelDef).optional(),
  memory_write: z.enum(["deny", "ask", "allow_principal", "allow"]).optional(),
  skills: z
    .array(
      z.strictObject({
        name: z.string(),
        description: z.string(),
        body: z.string(),
      }),
    )
    .optional(),
});
type Def = z.output<typeof Plain> & {
  readonly subagents?: readonly Def[] | undefined;
  readonly handoffs?: readonly Def[] | undefined;
  readonly team?: readonly Def[] | undefined;
};
const Def: z.ZodType<Def, unknown> = z.lazy(() =>
  Plain.extend({
    subagents: z.array(Def).optional(),
    handoffs: z.array(Def).optional(),
    team: z.array(Def).optional(),
  }),
);
const Vector = z.strictObject({
  description: z.string(),
  cases: z.array(
    z.strictObject({
      name: z.string(),
      agent: Def,
      member: z.string().optional(),
      dynamic: z
        .strictObject({ define: MemberDefine, starter: z.string() })
        .optional(),
      /** The host directory a `local_dir` workspace reads, written under the case's cwd. */
      directory: z.array(DirEntry).optional(),
      thread_started: z.record(z.string(), z.json()),
    }),
  ),
});
type Vector = z.output<typeof Vector>;
const vector = Vector.parse(
  JSON.parse(
    readFileSync(
      join(
        import.meta.dir,
        "../../../../../spec/conformance/vectors/agent-pins.json",
      ),
      "utf8",
    ),
  ),
);

/** A scripted model under another name, declaring a price and a cache lifetime. */
function model(d: z.output<typeof ModelDef>): Model {
  const base = scriptedModel({ responses: [] });
  const declared: Model = {
    ...base,
    info: {
      ...base.info,
      model: { provider: "scripted", name: d.name },
      limits: {
        ...base.info.limits,
        name: d.name,
        ...(d.price === undefined ? {} : { price: d.price }),
      },
      cache: d.cache_ttl_ms === undefined ? "none" : { ttl_ms: d.cache_ttl_ms },
    },
  };
  markTestKit(declared);
  return declared;
}

const noop = async (): Promise<void> => {};
const HOOKS = {
  session_end: { sessionEnd: noop },
  notification: { notification: noop },
};

function ext(d: z.output<typeof ExtensionDef>): Extension {
  return extension({
    name: d.name,
    ...(d.instructions === undefined ? {} : { instructions: d.instructions }),
    hooks: Object.assign({}, ...(d.hooks ?? []).map((h) => HOOKS[h])),
    on: Object.fromEntries((d.observers ?? []).map((o) => [o, noop])),
    ...(d.hook_timeout_ms === undefined
      ? {}
      : { hookTimeoutMs: d.hook_timeout_ms }),
  });
}

/** A dynamic agent: the vector's templates set nothing but models and a sandbox. */
function template(d: Def): DynamicAgent<never, unknown> {
  const models = Object.fromEntries(
    (d.models ?? []).map((m) => [m.key ?? m.name, model(m)]),
  );
  return dynamicAgent({
    name: d.name,
    instructions: d.instructions,
    models,
    ...(d.sandbox === undefined ? {} : { sandbox: fakeSandbox() }),
  });
}

/** The agents built for a lead's team, by name: a member case pins one of them. */
const members = new Map<string, object>();

/** The vector's workspace, with its wire keys as agent() spells them. */
function workspaceOf(w: z.output<typeof WorkspaceDef>): Workspace {
  return {
    ...(w.files === undefined ? {} : { files: w.files }),
    ...(w.local_dir === undefined ? {} : { localDir: w.local_dir }),
    ...(w.include === undefined ? {} : { include: w.include }),
  };
}

/**
 * Writes `entries` under `<scratch>/<at>` and runs in that scratch directory, so a `local_dir`
 * the vector wrote as "./app" is the same relative path in both languages.
 */
function inDirectory(
  entries: readonly z.output<typeof DirEntry>[],
  at: string,
): () => void {
  const scratch = mkdtempSync(join(tmpdir(), "threads-pins-"));
  const root = resolve(scratch, at);
  mkdirSync(root, { recursive: true });
  for (const e of entries) {
    const path = join(root, e.path);
    mkdirSync(dirname(path), { recursive: true });
    if (e.symlink !== undefined) symlinkSync(e.symlink, path);
    else if (e.dir === true) mkdirSync(path, { recursive: true });
    else
      writeFileSync(path, e.text ?? "", {
        mode: e.exec === true ? 0o755 : 0o644,
      });
  }
  const was = process.cwd();
  process.chdir(scratch);
  return () => {
    process.chdir(was);
    rmSync(scratch, { recursive: true, force: true });
  };
}

/** The fake sandbox, with the vector's egress when given. */
const sandboxed = (d: Def) =>
  d.sandbox === undefined
    ? {}
    : {
        sandbox: fakeSandbox(),
        ...(d.egress === undefined ? {} : { egress: d.egress }),
      };

/** The vector's agent. */
function build(d: Def): Agent<never, unknown> {
  const common = {
    name: d.name,
    instructions: d.instructions,
    model:
      d.model === undefined ? scriptedModel({ responses: [] }) : model(d.model),
    ...(d.fallback === undefined ? {} : { fallback: d.fallback.map(model) }),
    ...(d.permissions === undefined ? {} : { permissions: d.permissions }),
    ...(d.retry === undefined ? {} : { retry: d.retry }),
    ...(d.context === undefined ? {} : { context: d.context }),
    ...(d.budget === undefined ? {} : { budget: d.budget }),
    ...(d.on_unknown_usage === undefined
      ? {}
      : { onUnknownUsage: d.on_unknown_usage }),
    ...(d.output_styles === undefined ? {} : { outputStyles: d.output_styles }),
    ...(d.extensions === undefined
      ? {}
      : { extensions: d.extensions.map(ext) }),
    ...sandboxed(d),
    ...(d.workspace === undefined
      ? {}
      : { workspace: workspaceOf(d.workspace) }),
    ...(d.memory_write === undefined
      ? {}
      : { memory: localMemory(), memoryWrite: d.memory_write }),
    ...(d.skills === undefined ? {} : { skills: d.skills }),
    ...(d.subagents === undefined ? {} : { subagents: d.subagents.map(build) }),
    ...(d.handoffs === undefined ? {} : { handoffs: d.handoffs.map(build) }),
  };
  return d.team === undefined
    ? agent(common)
    : agent({
        ...common,
        team: d.team.map((m) => {
          const built = m.models === undefined ? build(m) : template(m);
          members.set(m.name, built);
          return built;
        }),
      });
}

/** Pinned by config_hash but not in thread_started (spec/schema/README.md, "The pinned config"). */
const HASHED_ONLY = [
  "extensions",
  "skills",
  "memory_write",
  "sandbox",
  "concurrent_tools",
  "dynamic",
];

/**
 * A team member's thread_started: its canonical config less the hashed-only fields. It inherits
 * `deferTools`, its lead's resolved defer_tools, unless it sets its own.
 */
async function memberPin(
  a: object,
  deferTools: DeferTools,
  choice: Vector["cases"][number]["dynamic"],
): Promise<unknown> {
  const pinned = await memberEntry(a)?.pinned(deferTools, choice);
  if (pinned === undefined) throw new Error("no member pin");
  const config = z
    .record(z.string(), z.json())
    .parse(JSON.parse(pinned.config));
  const started = Object.entries(config).filter(
    ([k]) => !HASHED_ONLY.includes(k),
  );
  return { ...Object.fromEntries(started), config_hash: pinned.configHash };
}

/** The pin one case expects: a member's, or the lead's own less its per-thread team ids. */
async function pinOf(c: Vector["cases"][number]): Promise<unknown> {
  const a = build(c.agent);
  const started = (await hostRunner(a)?.started())?.event;
  if (started?.type !== "thread_started") throw new Error("no pin");
  if (c.member === undefined) {
    const { team: _team, ...data } = started.data;
    return z.json().parse(data);
  }
  const member = members.get(c.member);
  if (member === undefined) throw new Error(`no member ${c.member}`);
  const lead = started.data.policy?.context?.defer_tools ?? "auto";
  return z.json().parse(await memberPin(member, lead, c.dynamic));
}

describe("agent pins", () => {
  for (const c of vector.cases)
    test(c.name, async () => {
      const done =
        c.directory === undefined
          ? undefined
          : inDirectory(c.directory, c.agent.workspace?.local_dir ?? ".");
      try {
        expect(await pinOf(c)).toEqual(c.thread_started);
      } finally {
        done?.();
      }
    });
});
