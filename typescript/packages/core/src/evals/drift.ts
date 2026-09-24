import type { DryPin } from "../agent/registry";
import type { EventOf } from "../fold/state";
import { canonicalize, parseStrictJson } from "../log";
import type { DriftCheck, ToolDiff } from "./schema";

// Drift (spec lane 22, B.3): the case's recorded config against the agent as it is pinned now,
// by a dry pin that runs no setup. The recorded prompt is line 0 of the case's first request;
// the model is the case's first settings epoch. What the dry pin can't see is `unchecked`, never
// stale.

/** A tool as pinned: the recorded one or the dry pin's draft of it. */
type Spec = {
  readonly name: string;
  readonly origin?: { readonly extension: string } | undefined;
};
/** A thread_started's data as drift reads it, recorded or freshly pinned. */
type Started = Pick<
  EventOf<"thread_started">["data"],
  "agent_name" | "config_hash" | "instructions"
> & {
  readonly parent?: { readonly relation: keyof typeof RELATIONS } | undefined;
  readonly model: unknown;
  readonly model_params: unknown;
  readonly adapter: unknown;
  readonly tools: readonly Spec[];
};

export type Recorded = {
  /** The case log's thread_started: the first settings epoch and the pinned tools. */
  readonly started: Started;
  /** line0.json (or a recorded request's line 0), when the case has one. */
  readonly line0: Uint8Array | undefined;
};

const RELATIONS = {
  subagent: "spawn",
  team_member: "team_member",
  handoff: "handoff",
} as const;

const text = (value: unknown): string => {
  const c = canonicalize(JSON.parse(JSON.stringify(value ?? null)));
  if (!c.ok) throw new Error("pinned values are canonical JSON");
  return c.value;
};

/** A pinned tool as drift compares it: everything but where it came from. */
const shape = ({ origin: _, ...spec }: Spec): string => text(spec);

/** The recorded line 0's system text, or undefined when there is no readable line 0. */
function recordedSystem(line0: Uint8Array | undefined): string | undefined {
  if (line0 === undefined) return undefined;
  const parsed = parseStrictJson(new TextDecoder().decode(line0));
  if (!parsed.ok || typeof parsed.value !== "object" || parsed.value === null)
    return undefined;
  const system: unknown = Reflect.get(parsed.value, "system");
  return typeof system === "string" ? system : undefined;
}

/** What the dry pin couldn't compare, as the report names it. */
function uncheckedOf(pin: DryPin): readonly string[] {
  return [
    ...pin.mcp.map((s) => `mcp:${s}`),
    ...pin.setupExtensions.map((e) => `extension:${e}`),
    ...pin.setupProviders,
  ];
}

/** Tools the dry pin can compare: no MCP tool, none a setup-bearing extension contributed. */
function comparable(
  tools: readonly Spec[],
  pin: DryPin,
): ReadonlyMap<string, string> {
  const setup = new Set(pin.setupExtensions);
  return new Map(
    tools
      .filter(
        (t) =>
          !pin.mcp.some((s) => t.name.startsWith(`mcp__${s}__`)) &&
          !setup.has(t.origin?.extension ?? ""),
      )
      .map((t) => [t.name, shape(t)]),
  );
}

function toolDiff(recorded: Started, pin: DryPin): ToolDiff | undefined {
  const before = comparable(recorded.tools, pin);
  const after = comparable(pin.started.tools, pin);
  const added = [...after.keys()].filter((n) => !before.has(n)).toSorted();
  const removed = [...before.keys()].filter((n) => !after.has(n)).toSorted();
  const changed = [...after]
    .filter(([n, s]) => before.has(n) && before.get(n) !== s)
    .map(([n]) => n)
    .toSorted();
  return added.length + removed.length + changed.length === 0
    ? undefined
    : { added, removed, changed };
}

/** A case saved before tool origins can't tell a setup-bearing extension's tools apart. */
const originless = (recorded: Started, pin: DryPin): boolean =>
  pin.setupExtensions.length > 0 &&
  !recorded.tools.some((t) => t.origin !== undefined);

function compare(recorded: Recorded, pin: DryPin): DriftCheck {
  const unchecked = [...uncheckedOf(pin)];
  const system = recordedSystem(recorded.line0);
  if (system === undefined) unchecked.push("no_recorded_prefix");
  const kinds: DriftCheck["kinds"] = [];
  const promptBlind =
    pin.setupExtensions.length > 0 || pin.setupProviders.length > 0;
  if (
    !promptBlind &&
    system !== undefined &&
    system !== pin.started.instructions
  )
    kinds.push("prompt");
  const tools = originless(recorded.started, pin)
    ? undefined
    : toolDiff(recorded.started, pin);
  if (tools !== undefined) kinds.push("tools");
  const settings = (s: Started) => text([s.model, s.model_params, s.adapter]);
  if (settings(recorded.started) !== settings(pin.started)) kinds.push("model");
  if (
    kinds.length === 0 &&
    unchecked.length === 0 &&
    recorded.started.config_hash !== pin.started.config_hash
  )
    kinds.push("config");
  return {
    ok: kinds.length === 0,
    kinds,
    ...(tools === undefined ? {} : { tools }),
    ...(unchecked.length === 0 ? {} : { unchecked }),
  };
}

/** The drift check of one case against the agents given. */
export function drift(recorded: Recorded, pins: readonly DryPin[]): DriftCheck {
  const relation = recorded.started.parent?.relation;
  if (relation !== undefined)
    return {
      ok: true,
      kinds: [],
      unchecked: [`relation:${RELATIONS[relation]}`],
    };
  const pin = pins.find(
    (p) => p.started.agent_name === recorded.started.agent_name,
  );
  if (pin === undefined)
    return {
      ok: false,
      kinds: [],
      agents: pins.map((p) => p.started.agent_name),
    };
  return compare(recorded, pin);
}
