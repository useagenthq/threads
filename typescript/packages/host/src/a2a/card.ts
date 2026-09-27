import {
  A2A_VERSION,
  AgentCard,
  type AgentInterface,
  IDEMPOTENT_SEND,
} from "@threadsai/a2a/protocol";
import { canonicalize } from "threadsai/host";
import type { Exposed, ExposedAgent } from "./config";

// The agent card, a pure function of the config and the request's own origin. A card is what we
// publish about an agent, so it is the one place where saying too much is the failure: no
// instructions, no tool names, no model name, ever. The interface URLs come from the origin the
// card was asked for, so there is no base-URL option to get wrong.

/**
 * The dedup window our extension declares. A receipt row is durable and never swept, so this is
 * a floor we can always keep rather than a limit we enforce; a peer may safely resend inside it.
 */
const DEDUP_WINDOW_MS = 7 * 24 * 60 * 60 * 1000;

/** Text and JSON only: a file part is refused where it arrives (ContentTypeNotSupportedError). */
const MODES: readonly string[] = ["text/plain", "application/json"];

/**
 * The requirements the declared schemes imply. Declaring a scheme and no requirement is what a
 * conforming partner reads as "nothing is required": it calls unauthenticated and gets our 401. Our
 * own client infers "auth required" from the scheme set being non-empty, which is exactly why this
 * was invisible from inside.
 *
 * One `SecurityRequirement` per declared scheme name, so satisfying any one of them is enough, and
 * the names come from what the operator declared rather than from `bearer` written here — a custom
 * scheme set gets its requirement too. `list` is the scopes, and none of ours has any.
 */
function requirements(
  schemes: Readonly<Record<string, unknown>>,
): readonly unknown[] | undefined {
  const names = Object.keys(schemes).toSorted();
  return names.length === 0
    ? undefined
    : names.map((name) => ({ schemes: { [name]: { list: [] } } }));
}

function agentCard(
  exposed: Exposed,
  name: string,
  agent: ExposedAgent,
  origin: string,
): AgentCard {
  const url = `${origin}/a2a/${encodeURIComponent(name)}`;
  const interfaces: readonly AgentInterface[] = [
    { url, protocolBinding: "JSONRPC", protocolVersion: A2A_VERSION },
    { url, protocolBinding: "HTTP+JSON", protocolVersion: A2A_VERSION },
  ];
  // Parsed, not cast: the card crosses a trust boundary outwards, so a card we cannot parse is a
  // bug in this function and fails here rather than at a partner.
  return AgentCard.parse({
    name: agent.hosted.name,
    description: agent.description,
    supportedInterfaces: interfaces,
    // The agent's pinned config hash: a card's version changes exactly when its agent does, and
    // a hash publishes nothing about the configuration it names.
    version: agent.configHash,
    capabilities: {
      streaming: true,
      // The cuts, stated in the card and not only in the docs.
      pushNotifications: false,
      extendedAgentCard: false,
      extensions: [
        {
          uri: IDEMPOTENT_SEND,
          description:
            "A SendMessage that repeats a messageId from the same authenticated caller returns the original task and starts nothing.",
          params: { window_ms: DEDUP_WINDOW_MS },
        },
      ],
    },
    securitySchemes: exposed.securitySchemes,
    securityRequirements: requirements(exposed.securitySchemes),
    defaultInputModes: MODES,
    defaultOutputModes: MODES,
    skills: [
      {
        id: name,
        name: agent.hosted.name,
        description: agent.description,
        tags: ["text"],
      },
    ],
  });
}

/** The card's bytes, canonical so the same config and origin always publish the same card. */
export function cardBytes(
  exposed: Exposed,
  name: string,
  agent: ExposedAgent,
  origin: string,
): string {
  const text = canonicalize(agentCard(exposed, name, agent, origin));
  if (!text.ok) throw new Error(`an agent card is JSON: ${text.error.message}`);
  return text.value;
}
