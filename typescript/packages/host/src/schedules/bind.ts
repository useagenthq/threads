import type { Input } from "@threads/core";
import { Name } from "@threads/core/host";
import type { HostContext, HostedAgent } from "../context";
import { type Cron, parseCron } from "../cron";

/** spec/api.json Schedule. */
export type Schedule = {
  readonly id: string;
  /** A key of host({agents}). */
  readonly agent: string;
  readonly cron: string;
  /** IANA name; default UTC. */
  readonly timezone?: string;
  readonly input: Input;
};

/** A configured schedule, checked against the host at ready(). */
export type Bound = {
  readonly schedule: Schedule;
  readonly cron: Cron;
  readonly hosted: HostedAgent;
  readonly timezone: string;
};

/** Each schedule's cron, agent and zone, or what is wrong with the first bad one. */
export function bindSchedules(
  ctx: HostContext,
  schedules: readonly Schedule[],
): readonly Bound[] | string {
  const bound: Bound[] = [];
  for (const schedule of schedules) {
    if (!Name.safeParse(schedule.id).success)
      return `schedule ${schedule.id}: an id is lowercase letters, digits and underscores, starting with a letter, up to 64`;
    const cron = parseCron(schedule.cron);
    if (!cron.ok) return `schedule ${schedule.id}: ${cron.error}`;
    const hosted = ctx.agents.get(schedule.agent);
    if (hosted === undefined)
      return `schedule ${schedule.id}: no agent ${schedule.agent}`;
    const timezone = schedule.timezone ?? "UTC";
    try {
      new Intl.DateTimeFormat("en-US", { timeZone: timezone });
    } catch {
      return `schedule ${schedule.id}: unknown time zone ${timezone}`;
    }
    bound.push({ schedule, cron: cron.value, hosted, timezone });
  }
  return bound;
}
