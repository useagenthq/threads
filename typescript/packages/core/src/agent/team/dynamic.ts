import type { z } from "zod";
import { Name } from "../../log";
import type { Model } from "../../model";
import { type AgentOptions, type Models, registerAs, resolve } from "../agent";
import { ConfigError } from "../errors";
import type { DynamicAgent } from "./types";

// dynamicAgent() (spec/api.json, lane 26): a template the code writes. A start chooses only a
// label, written instructions, a subset of its tools and one of its models; everything else is
// this config, pinned for every member started from it.

/** Every agent() option but model, team, subagents, handoffs and teamLimits, plus models. */
export type DynamicAgentOptions<Deps, Output> = Omit<
  AgentOptions<Deps, Output>,
  "model" | "team" | "subagents" | "handoffs" | "teamLimits"
> & {
  /** The models a start may choose, by key; the first key is the default. */
  readonly models: Readonly<Record<string, Model>>;
};

const NESTING = ["team", "subagents", "handoffs"] as const;

export function dynamicAgent<Deps = undefined>(
  options: DynamicAgentOptions<Deps, string> & { readonly output?: undefined },
): DynamicAgent<Deps, string>;
export function dynamicAgent<Deps, Output>(
  options: DynamicAgentOptions<Deps, Output> & {
    readonly output: z.ZodType<Output>;
  },
): DynamicAgent<Deps, Output>;
/** Throws ConfigError invalid_config for missing or badly keyed models, or a team of its own. */
export function dynamicAgent<Deps, Output>(
  options: DynamicAgentOptions<Deps, Output>,
): DynamicAgent<Deps, Output> {
  const models = modelsOf(options);
  const first = models[0];
  if (first === undefined) throw new Error("modelsOf returns at least one");
  const { output: schema, ...rest } = options;
  const named = { name: options.name ?? "agent" };
  if (schema === undefined) {
    const { def } = resolve<Deps, string>(
      { ...rest, model: first[1] },
      (text) => text,
      models,
    );
    registerAs(named, def, options.approvers);
    return named;
  }
  const { def } = resolve<Deps, Output>(
    { ...options, model: first[1] },
    (_text, accepted) => schema.parse(accepted),
    models,
  );
  registerAs(named, def, options.approvers);
  return named;
}

/** The models in order, checked: non-empty, each key a name. */
function modelsOf<Deps, Output>(
  options: DynamicAgentOptions<Deps, Output>,
): Models {
  const nested = NESTING.find((k) => k in options);
  if (nested !== undefined)
    throw new ConfigError(
      "invalid_config",
      `${nested}: a dynamic agent can't start or hand off to other agents`,
    );
  if ("model" in options)
    throw new ConfigError(
      "invalid_config",
      "a dynamic agent takes models, not model: models: {key: model}, the first the default",
    );
  const models = Object.entries(options.models ?? {});
  if (models.length === 0)
    throw new ConfigError(
      "invalid_config",
      "models: a dynamic agent needs at least one model, by key; the first is the default",
    );
  const bad = models.find(([k]) => !Name.safeParse(k).success);
  if (bad !== undefined)
    throw new ConfigError(
      "invalid_config",
      `models: key ${JSON.stringify(bad[0])} must be lowercase letters, digits and underscores, starting with a letter`,
    );
  return models;
}
