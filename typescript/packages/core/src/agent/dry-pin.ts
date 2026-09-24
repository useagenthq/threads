import { ThreadStartedData } from "../log";
import { ConfigError } from "./errors";
import { pin } from "./pin";
import { type DryPin, dryPinOf } from "./registry";
import type { Resolved } from "./run";

// The dry pin (spec lane 22, B.3): the agent's thread_started as a new thread would pin it,
// without running any setup. So it resolves no secret, opens no MCP connection and runs no
// extension or provider setup, and it says what it therefore couldn't see.

/** A resolved agent pinned without setup; MCP servers contribute no tools. */
export function dryOf<Deps, Output>(def: Resolved<Deps, Output>): DryPin {
  const pinned = pin({ ...def, mcp: [] });
  if (pinned.started.type !== "thread_started")
    throw new Error("pin() drafts a thread_started");
  return {
    started: ThreadStartedData.parse(pinned.started.data),
    mcp: def.servers.map((s) => s.name),
    setupExtensions: def.extensions.flatMap((e) =>
      e.setup === undefined ? [] : [e.name],
    ),
    leadsTeam: def.team !== undefined,
    setupProviders: [
      ...(def.memory?.setup === undefined ? [] : ["memory" as const]),
      ...(def.knowledge?.setup === undefined ? [] : ["knowledge" as const]),
    ],
  };
}

/** The dry pin of an agent() handle. Throws ConfigError for anything else, or a bad config. */
export function dryPin(agent: object): DryPin {
  const dry = dryPinOf(agent);
  if (dry === undefined)
    throw new ConfigError("invalid_config", "agents: expected agent() handles");
  return dry();
}
