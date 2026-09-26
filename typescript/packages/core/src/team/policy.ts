import type { z } from "zod";
import { ConfigError } from "../agent/errors";
import type { Budget } from "../log";
import { TEAM_TOOLS } from "./constants";
import type { PolicyOp } from "./request";

// The host's messagePolicy (spec/api.json host.message_policy and MessagePolicyRule): rules that
// decide after a team's grant and before default deny, for every op of every sender. They only
// add, so a team's own grant is never narrowed. Which team tools an agent is offered, and whether
// it is a lead at all, follow from its rules the same way.

export type MessagePolicyRule = {
  /** The acting agent. */
  readonly from: string;
  /** The target agent. */
  readonly to: string;
  /** What `from` may do to `to`; wait is monitor. */
  readonly allow: readonly PolicyOp[];
  /**
   * Caps each member `from` starts, whose budget is the smaller of this and start's, and each
   * turn a send or ask to a host member opens. Omitted: no cap beyond the agent's own.
   */
  readonly budget?: z.infer<typeof Budget>;
};

/** The team tool each op gives its `from`; `wait` is decided as `monitor`. */
const TOOLS: Readonly<Record<PolicyOp, readonly string[]>> = {
  start: ["start"],
  send: ["send"],
  ask: ["ask"],
  monitor: ["monitor", "wait"],
  cancel: ["cancel"],
};

/** The rules with `agent` as the acting side, in configured order. */
export function rulesFrom(
  rules: readonly MessagePolicyRule[],
  agent: string,
): readonly MessagePolicyRule[] {
  return rules.filter((r) => r.from === agent);
}

/** The rule that lets `from` do `op` to `to`, if any. One (from, to) pair exists at most once. */
export function ruleFor(
  rules: readonly MessagePolicyRule[],
  from: string,
  to: string,
  op: PolicyOp,
): MessagePolicyRule | undefined {
  return rules.find(
    (r) => r.from === from && r.to === to && r.allow.includes(op),
  );
}

/**
 * The team tools a pin offers: all seven for a lead (agent({team})) and for a team's member; for
 * an agent with host rules and no team of its own, one per op some rule with it as `from` allows,
 * so its line 0 never shows a tool that is always denied.
 */
export function teamTools(
  own: readonly string[] | undefined,
  member: boolean,
  rules: readonly MessagePolicyRule[],
): readonly string[] {
  if (own !== undefined || member) return TEAM_TOOLS;
  const ops = new Set(rules.flatMap((r) => r.allow));
  return [...ops].flatMap((op) => TOOLS[op]);
}

/** The agents an agent's rules let it start. A rule allowing `start` makes its `from` a lead. */
export function ruleStarts(
  rules: readonly MessagePolicyRule[],
): readonly string[] {
  return rules.filter((r) => r.allow.includes("start")).map((r) => r.to);
}

/**
 * Whether an agent runs as a lead: it has a team of its own, or a rule lets it start. An agent
 * whose rules target only host members is no lead; it is a caller, in no team (lane 29D).
 */
export function leads(
  team: readonly string[] | undefined,
  rules: readonly MessagePolicyRule[],
): boolean {
  return team !== undefined || ruleStarts(rules).length > 0;
}

/** The agents start may name: those the team lists, then those a rule adds, each once. */
export function startable(
  team: readonly string[] | undefined,
  rules: readonly MessagePolicyRule[],
): readonly string[] {
  const own = team ?? [];
  return [...own, ...ruleStarts(rules).filter((n) => !own.includes(n))];
}

/**
 * host({messagePolicy}) at setup: every `from` and `to` names a host agent, every `allow` says
 * something, and each (from, to) pair appears once.
 */
export function checkMessagePolicy(
  rules: readonly MessagePolicyRule[],
  agents: ReadonlySet<string>,
): void {
  const seen = new Set<string>();
  for (const rule of rules) {
    const at = `messagePolicy rule ${rule.from} -> ${rule.to}`;
    for (const side of [rule.from, rule.to])
      if (!agents.has(side))
        throw new ConfigError(
          "invalid_config",
          `${at}: ${side} is not a host agent; name one of ${[...agents].toSorted().join(", ")}`,
        );
    if (rule.allow.length === 0)
      throw new ConfigError(
        "invalid_config",
        `${at}: allow is empty; list what ${rule.from} may do, or drop the rule`,
      );
    const pair = `${rule.from}\u0000${rule.to}`;
    if (seen.has(pair))
      throw new ConfigError(
        "duplicate_name",
        `${at} is listed twice; put every op of one pair in one rule`,
      );
    seen.add(pair);
  }
}
