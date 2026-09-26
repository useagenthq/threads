import { block } from "../team/dynamic";
import { type MessagePolicyRule, startable } from "../team/policy";
import type { Extension } from "./extension";
import { memberEntry } from "./registry";
import { type Skill, skillListing } from "./skills";

// Line 0's instructions (Render v1): pinned per thread, so every part here is a pure function of
// the agent's definition.

/** What line 0's instructions are made of. */
export type InstructionParts = {
  readonly instructions: string;
  readonly extensions: readonly Extension<never>[];
  readonly skills: readonly Skill[];
  /** Agent names spawn_agent may start. */
  readonly subagents: readonly string[];
  /** Agent names handoff may target. */
  readonly handoffs: readonly string[];
  /** agent({team}): the agents start may name. */
  readonly team: readonly string[] | undefined;
  /** The host's messagePolicy rules with this agent as `from`: one allowing start adds its to. */
  readonly rules?: readonly MessagePolicyRule[];
  /** The agents behind team: a dynamic agent adds its line to the listing. */
  readonly members: readonly { readonly name: string }[];
  /** A dynamic agent's member: the block its starter wrote comes last. */
  readonly dynamic?: {
    readonly starter: string;
    readonly define: { readonly instructions?: string | undefined };
  };
};

/**
 * The base instructions, then each extension's, in declaration order, then the skill listing,
 * then the agents spawn_agent, handoff and start may name (with a line per dynamic agent), and,
 * last, a dynamic member's written block.
 */
export function instructions(o: InstructionParts): string {
  const listed = (label: string, names: readonly string[]): string[] =>
    names.length === 0 ? [] : [`${label}: ${names.join(", ")}.`];
  const written = o.dynamic?.define.instructions;
  return [
    o.instructions,
    ...o.extensions.flatMap((e) => e.instructions ?? []),
    ...skillListing(o.skills),
    ...listed("Subagents you can start with spawn_agent", o.subagents),
    ...listed("Agents you can hand the conversation to", o.handoffs),
    ...teamListing(startable(o.team, o.rules ?? []), o.members),
    ...(written === undefined || o.dynamic === undefined
      ? []
      : [block(o.dynamic.starter, written)]),
  ]
    .filter((t) => t !== "")
    .join("\n\n");
}

/** The team line, then one line per dynamic agent, in team order: what the lead may choose. */
function teamListing(
  names: readonly string[],
  members: readonly { readonly name: string }[],
): string[] {
  if (names.length === 0) return [];
  const lines = members.flatMap((m) => {
    const template = memberEntry(m)?.template;
    if (template === undefined) return [];
    const tools = template.listed();
    const models = template.models.map((k, i) =>
      i === 0 ? `${k} (default)` : k,
    );
    return [
      `${m.name} (you write its instructions; tools: ${tools.length === 0 ? "none" : tools.join(", ")}; models: ${models.join(", ")})`,
    ];
  });
  return [
    [
      `Agents you can start as team members with start: ${names.join(", ")}.`,
      ...lines,
    ].join("\n"),
  ];
}
