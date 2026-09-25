import type { Result } from "../../result";
import type { ArtifactStore } from "../../store/artifacts";
import {
  type Chosen,
  type InvalidDefinition,
  KEPT_TOOLS,
  type Resolved,
  resolveDefinition,
  type Template,
} from "../../team/dynamic";
import type { Listed } from "../../team/ops";
import type { TeamAgentPin, TeamRuntime } from "../types";

// What a start pins before its append (design §4.10, before the transaction), for the lead's
// start tool and the operator's team.start alike: the named agent's pin, or a dynamic agent's
// member as the start's chosen fields define it, with its config bytes stored.

export type StartPin = {
  /** The member's pin; undefined when the team lists no such agent. */
  readonly pinned: TeamAgentPin | undefined;
  readonly resolved: Result<Resolved, InvalidDefinition>;
};

const utf8 = new TextEncoder();

/** `starter` is the name the block says wrote it: the starting lead's, or `operator`. */
export async function startPin(
  pin: TeamRuntime["pin"],
  artifacts: Pick<ArtifactStore, "put">,
  args: Chosen & { readonly agent: string },
  starter: string,
): Promise<StartPin> {
  const listed = await pin(args.agent);
  const resolved = resolveDefinition(templateOf(listed), args);
  const define = resolved.ok ? resolved.value.define : undefined;
  const pinned =
    define === undefined ? listed : await pin(args.agent, { define, starter });
  if (pinned !== undefined) {
    await artifacts.put(utf8.encode(pinned.config));
    for (const bytes of pinned.artifacts) await artifacts.put(bytes);
  }
  return { pinned, resolved };
}

/** The start plan's listing: the one agent it pinned. */
export function listedOf(
  agent: string,
  pinned: TeamAgentPin | undefined,
): ReadonlyMap<string, Listed> {
  return new Map(
    pinned === undefined ? [] : [[agent, { configHash: pinned.configHash }]],
  );
}

/** A dynamic agent as a start sees it: its pin's tools but F, and its model keys. */
function templateOf(pin: TeamAgentPin | undefined): Template | undefined {
  if (pin?.models === undefined) return undefined;
  return {
    tools: pin.tools.filter((t) => !KEPT_TOOLS.has(t)),
    models: pin.models,
  };
}
