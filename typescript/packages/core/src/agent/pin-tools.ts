import { TEAM_TOOLS } from "../team/constants";
import { ConfigError } from "./errors";
import type { Extension } from "./extension";
import type { Tool } from "./tool";

// Which tool names a pin offers, and how an extension's and an MCP server's tools are namespaced.
// The pin itself (thread_started's data and its config_hash) is pin.ts's.

export const byName = (
  a: { readonly name: string },
  b: { readonly name: string },
): number => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0);

const TEAM = [
  "send_message",
  "team_task_claim",
  "team_task_create",
  "team_task_update",
];

/** What `agentTools` reads of a pin: the lists that decide which framework tools it offers. */
type ToolSources = {
  readonly tools: readonly { readonly name: string }[];
  readonly subagents: readonly unknown[];
  readonly handoffs: readonly unknown[];
};

/**
 * todo_write always; ask_user for an answerer; spawn and task-board tools with subagents;
 * handoff with targets; the team tools `team` names (team/policy.ts teamTools).
 */
export function agentTools(
  o: ToolSources,
  subagent: boolean,
  team: readonly string[],
  answerer: boolean,
): readonly string[] {
  return [
    "todo_write",
    ...(answerer ? ["ask_user"] : []),
    ...(o.subagents.length > 0 ? ["spawn_agent"] : []),
    ...(o.subagents.length > 0 || subagent ? TEAM : []),
    ...(o.handoffs.length > 0 ? ["handoff"] : []),
    ...team,
  ];
}

/** A team thread's own tools can't take a team tool's name, pinned yet or not. */
export function checkTeamNames<Deps>(o: {
  readonly tools: readonly { readonly name: string }[];
  readonly extensions: readonly Extension<Deps>[];
  readonly mcp: readonly Tool<unknown, unknown, unknown>[];
}): void {
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
  // Not model-visible: line 0 leaves it out, config_hash covers it (drift reads it).
  const origin = { extension: ext };
  return {
    name,
    ...(t.concurrent === undefined ? {} : { concurrent: t.concurrent }),
    ...(t.defer === undefined ? {} : { defer: t.defer }),
    spec: () => ({ ...t.spec(), name, origin }),
    bind: (env) => {
      const impl = t.bind(env);
      return { ...impl, spec: { ...impl.spec, name, origin } };
    },
  };
}
