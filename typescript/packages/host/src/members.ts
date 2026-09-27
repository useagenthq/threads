import { ConfigError, type MessagePolicyRule } from "@threads/core";
import {
  type DryPin,
  dryPin,
  type RestartPolicy,
  type ToolSpec,
} from "@threads/core/host";

// host({members}) at setup (spec/api.json host.members): which host agents run as one long-lived
// member per tenant, and every refusal that makes a shared member safe. Checked synchronously from
// each agent's dry pin, so host() throws before anything is stored.

/** How the host supervises one host member; `restart` and its window are read by lane 29E. */
export type HostMemberOptions = {
  readonly restart?: "on_failure" | "never";
  readonly maxRestarts?: number;
  readonly withinMs?: number;
};

/** The defaults of spec/api.json HostMemberOptions. */
export const MEMBER_DEFAULTS = {
  restart: "on_failure",
  maxRestarts: 3,
  withinMs: 60_000,
} as const;

/** A configured host member, with its options resolved. */
export type HostMember = {
  readonly agent: string;
  readonly restart: "on_failure" | "never";
  readonly maxRestarts: number;
  readonly withinMs: number;
};

/** The public option as the supervisor step and the log read it (rule 51's `policy`). */
export function restartPolicy(member: HostMember): RestartPolicy {
  return {
    restart: member.restart,
    max_restarts: member.maxRestarts,
    within_ms: member.withinMs,
  };
}

/** The shortest window a restart policy may name (api.json: less is invalid_config). */
const MIN_WINDOW_MS = 1000;

const refuse = (
  code: "invalid_config" | "handoff_in_team",
  why: string,
): never => {
  throw new ConfigError(code, why);
};

/** Every public positive-integer option takes the same check. */
function posInt(at: string, field: string, value: number | undefined): void {
  if (value === undefined) return;
  if (!Number.isSafeInteger(value) || value < 1)
    refuse(
      "invalid_config",
      `${at}: ${field} is ${value}; give a positive integer`,
    );
}

/**
 * The configured members, each option resolved, after every setup refusal. `agents` maps a host
 * agent's pinned name to its handle; `approversOf` says whether that agent declares approvers.
 */
export function checkHostMembers(
  members: Readonly<Record<string, HostMemberOptions>>,
  agents: ReadonlyMap<string, object>,
  approversOf: (name: string) => boolean,
): readonly HostMember[] {
  return Object.entries(members).map(([name, options]) => {
    const at = `members.${name}`;
    const handle = agents.get(name);
    if (handle === undefined)
      return refuse(
        "invalid_config",
        `${at} names no host agent; name one of ${[...agents.keys()].toSorted().join(", ")}`,
      );
    checkMemberAgent(at, name, dryPin(handle), approversOf(name));
    posInt(at, "maxRestarts", options.maxRestarts);
    posInt(at, "withinMs", options.withinMs);
    const withinMs = options.withinMs ?? MEMBER_DEFAULTS.withinMs;
    if (withinMs < MIN_WINDOW_MS)
      refuse(
        "invalid_config",
        `${at}: withinMs is ${withinMs}; a restart window is at least ${MIN_WINDOW_MS} ms`,
      );
    return {
      agent: name,
      restart: options.restart ?? MEMBER_DEFAULTS.restart,
      maxRestarts: options.maxRestarts ?? MEMBER_DEFAULTS.maxRestarts,
      withinMs,
    };
  });
}

/**
 * What the agent itself must be: no team of its own and no handoffs (it runs in the host team,
 * which starts nothing and hands off nowhere), compaction left on (it never ends, so its context
 * has to be compacted), and approvers whenever a turn of it could stop for a human.
 */
function checkMemberAgent(
  at: string,
  name: string,
  pinned: DryPin,
  approvers: boolean,
): void {
  if (pinned.leadsTeam)
    refuse(
      "invalid_config",
      `${at} has a team of its own; a host member runs in the host team and starts no members`,
    );
  if ((pinned.started.policy?.handoffs ?? []).length > 0)
    refuse(
      "handoff_in_team",
      `${at} lists handoffs, and a host member can't hand off; remove its handoffs or its place in members`,
    );
  if (compactionOff(pinned))
    refuse(
      "invalid_config",
      `${at} turns compaction off; a host member never ends, so its context has to be compacted`,
    );
  if (!approvers && needsApprovers(pinned.started.tools))
    refuse(
      "invalid_config",
      `a host member is shared by every conversation in the tenant, so its approvals need approvers; add approvers to agent '${name}'`,
    );
}

/**
 * Compaction is off when its trigger can never be reached: the whole effective window. There is no
 * other way to turn it off, so this is the check.
 */
function compactionOff(pinned: DryPin): boolean {
  const trigger = pinned.started.policy?.context?.compact.trigger;
  return (
    trigger !== undefined && "permille" in trigger && trigger.permille >= 1000
  );
}

/** A tool whose turn could stop for a human: a write of any kind, or ask_user itself. */
function needsApprovers(tools: readonly ToolSpec[]): boolean {
  return tools.some(
    (t) => t.name === "ask_user" || t.effect_class !== "read_only",
  );
}

/**
 * One predicate for the rules 29C left unbuilt because they need host({members}): **a rule with a
 * host member on either side allows only `send` and `ask`.** It covers all three cases:
 * nothing starts, monitors or cancels a host member (host({members}) starts it, Host.team stops
 * it); one host member may only send to or ask another; and a host member owns no members, so a
 * rule *from* it that allowed `start` would make it a lead as well (`leads()` in team/policy.ts),
 * which is exactly what refusing a host member with a `team` exists to prevent — and the dry pin
 * can't see rule-derived leadership.
 */
export function checkMemberRules(
  rules: readonly MessagePolicyRule[],
  members: ReadonlySet<string>,
): void {
  for (const rule of rules) {
    const to = members.has(rule.to);
    const from = members.has(rule.from);
    if (!to && !from) continue;
    const bad = rule.allow.filter((op) => op !== "send" && op !== "ask");
    if (bad.length === 0) continue;
    refuse(
      "invalid_config",
      `messagePolicy rule ${rule.from} -> ${rule.to}: ${why(from, to)}, so it allows only send and ask, not ${bad.join(", ")}`,
    );
  }
}

function why(from: boolean, to: boolean): string {
  if (from && to) return "one host member may only send to or ask another";
  return to
    ? "a host member is started by host({members}) and stopped by Host.team"
    : "a host member owns no members to start, monitor or cancel, and a start rule would make it a lead as well";
}
