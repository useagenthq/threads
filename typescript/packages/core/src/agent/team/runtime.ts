import { z } from "zod";
import type { TeamAgentPin, TeamRuntime } from "../../loop";
import { knownEvents } from "../../reduce";
import type { EventDraft, Writer } from "../../store";
import { uuidv7 } from "../../store/encode";
import { TEAM_TOOLS } from "../../team/constants";
import type { DeferTools } from "../defer";
import { ConfigError } from "../errors";
import { memberEntry } from "../registry";
import type { Plan, Resolved } from "../run";
import type { OpenStore } from "../sqlite";
import { recipientOf, runCovering } from "./budgets";
import { TeamWorker } from "./worker";

// A team thread's side of a run (spec/schema/README.md, "Teams"): the lead's first append names
// its new team; its loop gets the team tools' runtime; and the lead of an in-process run drives
// its team's worker for as long as the run is open.

/** A lead's thread_started names a new team, whose log its first append opens. */
export function leadStarted<Deps, Output>(
  def: Resolved<Deps, Output>,
  started: EventDraft,
  now: number,
): EventDraft {
  if (def.team === undefined || started.type !== "thread_started")
    return started;
  return { ...started, data: { ...started.data, team: newTeam(now) } };
}

/**
 * The same lead's thread_started for another new thread: a new team each time, since a team
 * belongs to one lead thread (a pin reused across threads keeps everything else).
 */
export function renewTeam(started: EventDraft, now: number): EventDraft {
  if (started.type !== "thread_started" || started.data.team === undefined)
    return started;
  return { ...started, data: { ...started.data, team: newTeam(now) } };
}

function newTeam(now: number): {
  readonly id: string;
  readonly log_thread_id: string;
  readonly log_branch_id: string;
} {
  return {
    id: uuidv7(now),
    log_thread_id: uuidv7(now),
    log_branch_id: uuidv7(now),
  };
}

/** A team thread's runtime and what stops it; undefined for any other thread. */
export function teamOf<Deps, Output>(
  def: Resolved<Deps, Output>,
  plan: Plan<Deps>,
  writer: Writer,
  opened: OpenStore,
):
  | { readonly runtime: TeamRuntime; readonly stop: () => Promise<void> }
  | undefined {
  if (def.team === undefined && plan.member === undefined) return undefined;
  // Members inherit the lead's resolved defer_tools unless they set their own.
  const deferTools = writer.chain.fold.policy?.context?.defer_tools;
  const base = {
    pin: pins(def, deferTools),
    limits: def.teamLimits,
    recipient: recipientOf(opened.log, opened.artifacts),
  };
  if (plan.member !== undefined)
    return {
      runtime: {
        ...base,
        notify: plan.member.notify,
        runCovering: runCovering(opened.log),
        principal: plan.principal,
      },
      stop: async () => undefined,
    };
  const started = knownEvents(writer.chain).find(
    (e) => e.type === "thread_started",
  );
  const team =
    started?.type === "thread_started" ? started.data.team?.id : undefined;
  if (team === undefined) return undefined;
  const worker = new TeamWorker({
    store: plan.store,
    log: opened.log,
    artifacts: opened.artifacts,
    team,
    agents: agentsOf(def.members),
    ...(plan.signal === undefined ? {} : { signal: plan.signal }),
    ...(deferTools === undefined ? {} : { deferTools }),
  });
  worker.start();
  return {
    runtime: {
      ...base,
      notify: worker.notify,
      progress: worker.progress,
      busy: worker.busy,
    },
    stop: () => worker.stop(),
  };
}

/**
 * The pins of the agents start may name, each made once, on first use; a dynamic member's each
 * time, for its starter's choice. Each inherits the lead's resolved defer_tools unless it sets
 * its own.
 */
function pins<Deps, Output>(
  def: Resolved<Deps, Output>,
  deferTools: DeferTools | undefined,
): TeamRuntime["pin"] {
  const made = new Map<string, Promise<TeamAgentPin | undefined>>();
  const entryOf = (agent: string) => {
    const handle = def.members.find((a) => a.name === agent);
    return handle === undefined ? undefined : memberEntry(handle);
  };
  return (agent, choice) => {
    if (choice !== undefined)
      return (
        entryOf(agent)?.pinned(deferTools, choice) ?? Promise.resolve(undefined)
      );
    const known = made.get(agent);
    if (known !== undefined) return known;
    const pinned =
      entryOf(agent)?.pinned(deferTools) ?? Promise.resolve(undefined);
    made.set(agent, pinned);
    return pinned;
  };
}

/**
 * Every agent of a team tree by name, nested teams included: materialize rebinds a member by its
 * agent name. Two agents of one name are refused (duplicate_name).
 */
export function agentsOf(
  team: readonly object[],
  into: Map<string, object> = new Map(),
): Map<string, object> {
  for (const agent of team) {
    const name = z.object({ name: z.string() }).parse(agent).name;
    const known = into.get(name);
    if (known === agent) continue;
    if (known !== undefined)
      throw new ConfigError(
        "duplicate_name",
        `two agents in one team are named ${name}; give each a unique name`,
      );
    into.set(name, agent);
    agentsOf(memberEntry(agent)?.team ?? [], into);
  }
  return into;
}

/** The starter a block names for Team.start: no agent in a team may take its name. */
const OPERATOR = "operator";

/** A dynamic agent runs only as a team member: never as a subagent or a handoff target. */
export function checkNotTemplates(agents: readonly object[]): void {
  for (const agent of agents)
    if (memberEntry(agent)?.template !== undefined) {
      const { name } = z.object({ name: z.string() }).parse(agent);
      throw new ConfigError(
        "invalid_config",
        `dynamic agents run as team members; put ${name} in team`,
      );
    }
}

/**
 * Setup refusals of a team (spec/api.json agent.team): a listed agent that hands off
 * (handoff_in_team), a listed agent's own tool named like a team tool, two agents of one
 * name in the tree (duplicate_name), and `operator`, reserved for an operator's start's block.
 */
export function checkTeam(
  lead: string,
  team: readonly object[] | undefined,
): void {
  if (team === undefined) return;
  const agents = agentsOf(team);
  if (lead === OPERATOR || agents.has(OPERATOR))
    throw new ConfigError(
      "invalid_config",
      `the name ${OPERATOR} is reserved in teams; rename that agent`,
    );
  for (const [name, agent] of agents) {
    const entry = memberEntry(agent);
    if (entry?.handsOff === true)
      throw new ConfigError(
        "handoff_in_team",
        `agent ${name} lists handoffs, and a team member can't hand off; remove its handoffs or its place in the team`,
      );
    const taken = entry?.toolNames.find((t) => TEAM_TOOLS.includes(t));
    if (taken !== undefined)
      throw new ConfigError(
        "duplicate_name",
        `tool ${taken} of agent ${name}: an agent in a team can't have a tool named ${TEAM_TOOLS.join(", ")}; rename it`,
      );
  }
}
