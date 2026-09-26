import { sha256Hex } from "@threads/core/adapter";
import { AgentCard, schemeKind } from "./card";
import { fetchBytes, type Sending } from "./client";
import { speaks1_0 } from "./version";
import { type Binding, bindingOf, type Wire } from "./wire";

// Fetching a card and choosing what to speak, once. Whoever pins the result (a member's config
// bytes, or a thread's remote_card event) keeps the bytes, so a card that changes later never
// moves a conversation that has already started.

/** Why a remote cannot be used. Each is a value, never a throw, and each names the fix. */
export type PinFailure =
  | { readonly code: "remote_unavailable"; readonly message: string }
  | { readonly code: "remote_unsupported"; readonly message: string }
  | { readonly code: "remote_auth_unsupported"; readonly message: string };

export type PinnedCard = {
  readonly bytes: Uint8Array;
  readonly sha256: string;
  readonly card: AgentCard;
  readonly wire: Wire;
  /** `window_ms` from our idempotent-send extension, when the card declares it. */
  readonly dedupWindowMs: number | undefined;
  readonly streaming: boolean;
};

export const IDEMPOTENT_SEND =
  "https://threadsai.dev/a2a/ext/idempotent-send/v1";
export const PROVENANCE = "https://threadsai.dev/a2a/provenance/v1";

/** The bindings we speak, in the order we prefer them when a card offers both. */
const PREFERRED: readonly Binding[] = ["JSONRPC", "HTTP+JSON"];

/** A card over this many bytes is refused before it is parsed. */
export const MAX_CARD_BYTES: number = 256 * 1024;

export async function fetchCard(
  cardUrl: string,
  hasBearer: boolean,
  sending: Sending,
): Promise<PinnedCard | PinFailure> {
  const got = await fetchBytes(cardUrl, MAX_CARD_BYTES, sending);
  return got.kind === "failed"
    ? {
        code: "remote_unavailable",
        message: `${cardUrl} could not be read: ${got.why}`,
      }
    : pinCard(got.bytes, cardUrl, hasBearer);
}

/**
 * A card's exact bytes, parsed and checked. Kept separate from the fetch so materialize and a
 * rebind can verify pinned bytes without the network, which is what makes replay hermetic.
 */
export function pinCard(
  bytes: Uint8Array,
  cardUrl: string,
  hasBearer: boolean,
): PinnedCard | PinFailure {
  let json: unknown;
  try {
    json = JSON.parse(
      new TextDecoder(undefined, { fatal: true }).decode(bytes),
    );
  } catch {
    return {
      code: "remote_unavailable",
      message: `${cardUrl} is not a JSON agent card`,
    };
  }
  const parsed = AgentCard.safeParse(json);
  if (!parsed.success)
    return {
      code: "remote_unavailable",
      message: `${cardUrl} is not an A2A 1.0 agent card: ${parsed.error.message}`,
    };
  const card = parsed.data;
  const chosen = choose(card);
  if (chosen === undefined)
    return {
      code: "remote_unsupported",
      message: `${card.name} declares no 1.0 interface in a binding we speak (${offered(card)})`,
    };
  const auth = satisfiable(card, hasBearer);
  if (auth !== undefined) return auth;
  return {
    bytes,
    sha256: sha256Hex(bytes),
    card,
    wire: chosen,
    dedupWindowMs: windowOf(card),
    streaming: card.capabilities.streaming === true,
  };
}

/** The first interface that speaks 1.0 in a binding we speak, preferring JSON-RPC. */
function choose(card: AgentCard): Wire | undefined {
  for (const want of PREFERRED) {
    const found = card.supportedInterfaces.find(
      (i) =>
        speaks1_0(i.protocolVersion) && bindingOf(i.protocolBinding) === want,
    );
    if (found !== undefined) return { url: found.url, binding: want };
  }
  return undefined;
}

function offered(card: AgentCard): string {
  return card.supportedInterfaces
    .map((i) => `${i.protocolBinding} ${i.protocolVersion}`)
    .join(", ");
}

/**
 * Whether the credential we hold can satisfy the card. A card that declares no scheme asks for
 * nothing, so an unauthenticated call is what it wants. Otherwise one scheme must be an HTTP
 * `bearer`, because `bearer` is the only auth helper (decision 2).
 */
function satisfiable(
  card: AgentCard,
  hasBearer: boolean,
): PinFailure | undefined {
  const declared = Object.entries(card.securitySchemes ?? {});
  if (declared.length === 0) return undefined;
  const bearer = declared.some(
    ([, s]) =>
      schemeKind(s) === "httpAuthSecurityScheme" &&
      s.httpAuthSecurityScheme?.scheme.toLowerCase() === "bearer",
  );
  if (bearer && hasBearer) return undefined;
  const names = declared
    .map(([name, s]) => `${name} (${schemeKind(s) ?? "unrecognised"})`)
    .join(", ");
  return {
    code: "remote_auth_unsupported",
    message: bearer
      ? `${card.name} requires a bearer token; pass auth: bearer(secret("…"))`
      : `${card.name} declares only ${names}; bearer is the only scheme this adapter sends`,
  };
}

/** `window_ms` from our extension, when the card declares it with a usable value. */
function windowOf(card: AgentCard): number | undefined {
  const found = card.capabilities.extensions?.find(
    (e) => e.uri === IDEMPOTENT_SEND,
  );
  const raw = found?.params?.["window_ms"];
  return typeof raw === "number" && Number.isInteger(raw) && raw > 0
    ? raw
    : undefined;
}
