import {
  A2A_JSON,
  type Binding,
  checkVersion,
  fault,
} from "@threads/a2a/protocol";
import { Principal } from "@threads/core/host";
import type { HostContext } from "../context";
import type { Authenticate } from "../http";
import { cardBytes } from "./card";
import type { Exposed } from "./config";
import { runOperation } from "./dispatch";
import {
  type Inbound,
  operation,
  requestOf,
  tenantMismatch,
  unnamed,
} from "./parse";
import { refuse, unauthenticated, versionOf } from "./wire";

// The A2A routes, mounted before the /v1 table. The card is discovery and takes no version and no
// principal. On every other route the order is fixed: version, then authenticate, then whether the
// agent is exposed, then the tenant, then the operation — so an agent this host does not expose
// answers exactly as a path that does not exist, and a caller cannot probe which agents we run.

/**
 * Undefined when the path is none of ours, so the host's other routes still see the request. An
 * unset `exposed` is a host without the option, or one whose ready() has not run: it serves
 * nothing, because nothing has been checked yet.
 */
export async function a2aRoute(
  ctx: HostContext,
  exposed: Exposed | undefined,
  authenticate: Authenticate | undefined,
  request: Request,
): Promise<Response | undefined> {
  if (exposed === undefined) return undefined;
  const url = new URL(request.url);
  if (url.pathname === "/.well-known/agent-card.json")
    return request.method === "GET" ? soleCard(exposed, url.origin) : undefined;
  if (!url.pathname.startsWith("/a2a/")) return undefined;
  const rest = url.pathname.slice("/a2a".length);
  const slash = rest.indexOf("/", 1);
  const name = decoded(slash === -1 ? rest.slice(1) : rest.slice(1, slash));
  const path = slash === -1 ? "" : rest.slice(slash);
  if (name === undefined) return notFound("HTTP+JSON");
  if (path === "/.well-known/agent-card.json")
    return request.method === "GET"
      ? card(exposed, name, url.origin)
      : notFound("HTTP+JSON");
  return dispatch(ctx, exposed, authenticate, {
    request,
    url,
    name,
    path,
    binding: path === "" ? "JSONRPC" : "HTTP+JSON",
  });
}

/**
 * The single-agent alias. Served only when exactly one agent is exposed: with two, there is no
 * answer that is not a guess, so the path is not ours and the host answers its own not_found.
 */
function soleCard(exposed: Exposed, origin: string): Response | undefined {
  const only = [...exposed.agents.keys()];
  const name = only.length === 1 ? only[0] : undefined;
  // The alias is only where the card was found; the endpoints it names are the agent's own.
  return name === undefined ? undefined : card(exposed, name, origin);
}

function card(exposed: Exposed, name: string, origin: string): Response {
  const agent = exposed.agents.get(name);
  if (agent === undefined) return notFound("HTTP+JSON");
  return new Response(cardBytes(exposed, name, agent, origin), {
    status: 200,
    headers: { "content-type": A2A_JSON, "cache-control": "no-cache" },
  });
}

function notFound(binding: Binding): Response {
  return refuse(
    { binding, id: undefined },
    fault("MethodNotFoundError", "no such A2A operation on this host"),
  );
}

function decoded(raw: string): string | undefined {
  try {
    return decodeURIComponent(raw);
  } catch {
    return undefined;
  }
}

async function dispatch(
  ctx: HostContext,
  exposed: Exposed,
  authenticate: Authenticate | undefined,
  inbound: Inbound,
): Promise<Response> {
  // The version and the caller come from headers, so both are settled before the body is read: an
  // unauthenticated caller never gets us to parse a payload. The cost is that these two refusals
  // cannot echo a JSON-RPC id, which is only known once the body is parsed; a client that sent a
  // bad version or no credential has its answer either way.
  const { header, query } = versionOf(inbound.url, inbound.request);
  const bare = { binding: inbound.binding, id: undefined } as const;
  const wrong = checkVersion(header, query);
  if (wrong !== undefined) return refuse(bare, wrong);
  // authenticate is the app's own code, so its answer is parsed, never trusted as typed.
  const raw =
    authenticate === undefined ? null : await authenticate(inbound.request);
  const who = Principal.safeParse(raw);
  if (raw === null || !who.success) return unauthenticated(bare);
  const parsed = await operation(inbound);
  if (unnamed(parsed)) return refuse(parsed.envelope, parsed.fault);
  const { envelope, method, params } = parsed;
  const agent = exposed.agents.get(inbound.name);
  if (agent === undefined) return notFound(inbound.binding);
  const request = requestOf(method, params);
  if (!request.ok) return refuse(envelope, request.fault);
  const foreign = tenantMismatch(request.value, who.data);
  if (foreign !== undefined) return refuse(envelope, foreign);
  return runOperation(ctx, who.data, inbound, envelope, agent, method, params);
}
