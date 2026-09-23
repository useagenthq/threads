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
import type { Model } from "../model";
import { DEFAULT_PERMISSIONS } from "../permissions";
import type { Sandbox } from "../sandbox";
import type { EventDraft } from "../store";
import { builtins, type Egress } from "../tools";
import { frameworkSpec } from "../tools/framework";
import { ConfigError } from "./errors";
import { type Extension, hookNames } from "./extension";
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
  readonly extensions: readonly Extension<never>[];
};

/** The pinned tool specs and the thread_started draft. Throws ConfigError on a bad setup. */
export function pin(o: PinOptions): {
  readonly specs: readonly ToolSpec[];
  readonly started: EventDraft;
} {
  const specs = [
    ...[
      ...builtins(o.sandbox, o.egress).map((b) => b.spec),
      frameworkSpec("todo_write"),
    ].toSorted((a, b) => (a.name < b.name ? -1 : 1)),
    ...o.tools.map((t) => t.spec()),
    ...extensionTools(o.extensions).map((t) => t.spec()),
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
      data: { ...cfg, config_hash: sha256Hex(text.value) },
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

/** the base instructions, then each extension's, in declaration order. */
function instructions(o: PinOptions): string {
  return [o.instructions, ...o.extensions.flatMap((e) => e.instructions ?? [])]
    .filter((t) => t !== "")
    .join("\n\n");
}

/** Extension tools, namespaced <ext>__<tool> and sorted by that name. */
export function extensionTools<Deps>(
  extensions: readonly Extension<Deps>[],
): readonly Tool<unknown, unknown, Deps>[] {
  return extensions
    .flatMap((e) => (e.tools ?? []).map((t) => namespaced(t, e.name)))
    .toSorted((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0));
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
    ...(o.output === undefined
      ? {}
      : { output: outputPolicy(o.output, o.outputRetries) }),
  };
}

function outputPolicy(
  schema: z.ZodType,
  maxRetries: number,
): NonNullable<Policy["output"]> {
  const exported = jsonSchema("output", schema);
  const text = canonicalize(exported);
  if (!text.ok) throw new ConfigError("invalid_config", text.error.message);
  return {
    schema: exported,
    schema_sha256: sha256Hex(text.value),
    mode: "tool",
    max_retries: maxRetries,
  };
}
