import { sha256Hex } from "../../hash";
import { ThreadStartedData } from "../../log";
import { knownEvents } from "../../reduce";
import { type DynamicChoice, KEPT_TOOLS } from "../../team/dynamic";
import type { Agent } from "../agent";
import { Unbound } from "../errors";
import { execute } from "../execute";
import { pin } from "../pin";
import { type MemberEntry, memberEntry } from "../registry";
import type { RunResult } from "../result";
import { pinnedAfterSetup, type Resolved } from "../run";
import { openStore } from "../sqlite";
import { teamHandle } from "./handle";
import type { Team, TeamAgent, TeamRunResult } from "./types";

// A lead's handle and what a team needs of each agent it lists.

const utf8 = new TextEncoder();

/** The TeamAgent over an agent's plain handle: every run's result carries the team. */
export function teamAgent<Deps, Output>(
  plain: Agent<Deps, Output>,
): TeamAgent<Deps, Output> {
  const handle: TeamAgent<Deps, Output> = {
    name: plain.name,
    check: plain.check,
    run: async (input, options) =>
      withTeam(handle, await plain.run(input, options)),
    stream: (input, options) => {
      const inner = plain.stream(input, options);
      return {
        [Symbol.asyncIterator]: () => inner[Symbol.asyncIterator](),
        result: (async () => withTeam(handle, await inner.result))(),
      };
    },
  };
  return handle;
}

/**
 * The result with the lead's team: the team its thread_started names, in the store's tenant,
 * acting as the principal of the run's request (its latest user_input).
 */
async function withTeam<Output>(
  lead: object,
  result: RunResult<Output>,
): Promise<TeamRunResult<Output>> {
  const { log, artifacts } = await openStore(result.thread.store);
  const read = await log.read(result.thread.branch);
  const events = read.ok ? knownEvents(read.value) : [];
  const started = events.find((e) => e.type === "thread_started");
  const input = events.findLast((e) => e.type === "user_input");
  const id =
    started?.type === "thread_started" ? started.data.team?.id : undefined;
  const entry = memberEntry(lead);
  if (id === undefined || input?.type !== "user_input" || entry === undefined)
    throw new Error(`thread ${result.thread.id} leads no team`);
  const team: Team = await teamHandle({
    log,
    artifacts,
    ref: { tenant: log.tenant, id },
    principal: input.actor.principal,
    lead: entry,
  });
  return { ...result, team };
}

/** What a team needs of an agent it lists: its pin as a member, and a runner for its branches. */
export function memberOf<Deps, Output>(
  def: Resolved<Deps, Output>,
): MemberEntry {
  const keys = def.models?.map(([k]) => k);
  return {
    handsOff: def.handoffs.length > 0,
    toolNames: def.tools.map((t) => t.name),
    team: def.team === undefined ? undefined : def.members,
    configHash: async (as) => {
      const { config } = await pinnedAfterSetup(
        def,
        as.member,
        as.deferTools,
        as.answerer,
      );
      return sha256Hex(utf8.encode(config));
    },
    teamLimits: def.teamLimits,
    ...(keys === undefined
      ? {}
      : {
          template: {
            models: keys,
            listed: () =>
              pin({ ...def, mcp: [] }, undefined, true)
                .specs.map((s) => s.name)
                .filter((n) => !KEPT_TOOLS.has(n)),
          },
        }),
    pinned: async (deferTools, choice) => {
      const member = choice === undefined ? def : memberDef(def, choice);
      const { config, started, artifacts } = await pinnedAfterSetup(
        member,
        true,
        deferTools,
      );
      if (started.type !== "thread_started")
        throw new Error("a pin is a thread_started");
      const pinned = ThreadStartedData.parse(started.data);
      const { model, model_params: params, policy } = pinned;
      return {
        configHash: sha256Hex(utf8.encode(config)),
        config,
        model,
        params,
        policy,
        budget: def.budget,
        tools: pinned.tools.map((t) => t.name),
        ...(keys === undefined ? {} : { models: keys }),
        artifacts,
      };
    },
    run: async (env) => {
      await execute(
        env.dynamic === undefined ? def : memberDef(def, env.dynamic),
        {
          store: env.store,
          principal: env.principal,
          thread: env.thread,
          holder: env.holder,
          member: { parent: env.parent, notify: env.notify },
          ...(env.deferTools === undefined
            ? {}
            : { deferTools: env.deferTools }),
          covering: env.covering,
          ...(env.signal === undefined ? {} : { signal: env.signal }),
        },
        [],
      );
    },
  };
}

/**
 * A member's definition: its template's, with the chosen model and the choice its pin binds.
 * A model key the template no longer has is ConfigError, which a rebind records as
 * pin_unavailable.
 */
export function memberDef<Deps, Output>(
  def: Resolved<Deps, Output>,
  choice: DynamicChoice,
): Resolved<Deps, Output> {
  const model = def.models?.find(([k]) => k === choice.define.model)?.[1];
  if (model === undefined)
    throw new Unbound(
      "invalid_config",
      `dynamic agent ${def.name} has no model ${choice.define.model}`,
    );
  return {
    ...def,
    model,
    dynamic: {
      template: def.name,
      define: choice.define,
      starter: choice.starter,
    },
  };
}
