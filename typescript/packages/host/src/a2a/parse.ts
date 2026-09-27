import {
  type A2aFault,
  type Binding,
  CancelTaskRequest,
  fault,
  GetTaskRequest,
  HTTP,
  inboundPath,
  ListTasksRequest,
  type Method,
  methodOf,
  parseJson,
  RpcRequest,
  SendMessageRequest,
  SubscribeToTaskRequest,
} from "@threadsai/a2a/protocol";
import type { Principal } from "threadsai/host";
import { cutOperation, cutPath } from "./cuts";
import type { Envelope } from "./wire";

// What operation a request names, in either binding, and its request message. The envelope an
// answer will use is settled before anything here can fail, so even a malformed body is refused in
// the caller's own binding rather than in whichever one we guessed.

export type Inbound = {
  readonly request: Request;
  readonly url: URL;
  readonly name: string;
  /** The path under `/a2a/{agent}`; empty for the JSON-RPC endpoint. */
  readonly path: string;
  readonly binding: Binding;
};

export type Named = {
  readonly envelope: Envelope;
  readonly method: Method;
  readonly params: unknown;
};

export type Parsed =
  | { readonly envelope: Envelope; readonly fault: A2aFault }
  | Named;

export function unnamed(parsed: Parsed): parsed is {
  readonly envelope: Envelope;
  readonly fault: A2aFault;
} {
  return "fault" in parsed;
}

export function operation(inbound: Inbound): Promise<Parsed> {
  return inbound.binding === "JSONRPC"
    ? rpcOperation(inbound)
    : httpOperation(inbound);
}

const SCHEMAS = {
  SendMessage: SendMessageRequest,
  SendStreamingMessage: SendMessageRequest,
  GetTask: GetTaskRequest,
  ListTasks: ListTasksRequest,
  CancelTask: CancelTaskRequest,
  SubscribeToTask: SubscribeToTaskRequest,
} as const satisfies Readonly<Record<Method, unknown>>;

/** Every request message shares a `tenant` field, so one parse covers all six operations. */
export type AnyRequest = { readonly tenant?: string | undefined };

/** The request message, parsed at the boundary, or why it is not one. */
export function requestOf(
  method: Method,
  params: unknown,
):
  | { readonly ok: true; readonly value: AnyRequest }
  | { readonly ok: false; readonly fault: A2aFault } {
  const parsed = SCHEMAS[method].safeParse(params);
  return parsed.success
    ? { ok: true, value: parsed.data }
    : { ok: false, fault: fault("InvalidParamsError", parsed.error.message) };
}

/** A request may carry the A2A tenant field only as the caller's own tenant. */
export function tenantMismatch(
  params: AnyRequest,
  principal: Principal,
): A2aFault | undefined {
  return params.tenant === undefined || params.tenant === principal.tenant
    ? undefined
    : fault(
        "InvalidParamsError",
        `tenant ${params.tenant} is not this caller's tenant`,
      );
}

const NOT_AN_OPERATION = fault(
  "MethodNotFoundError",
  "no such A2A operation on this host",
);

async function rpcOperation(inbound: Inbound): Promise<Parsed> {
  const envelope: Envelope = { binding: "JSONRPC", id: undefined };
  if (inbound.request.method !== "POST")
    return { envelope, fault: NOT_AN_OPERATION };
  const body = parseJson(await inbound.request.text());
  if (!body.ok) return { envelope, fault: body.fault };
  const rpc = RpcRequest.safeParse(body.value);
  if (!rpc.success)
    return {
      envelope,
      fault: fault("InvalidRequestError", "not a JSON-RPC 2.0 request"),
    };
  const named: Envelope = { binding: "JSONRPC", id: rpc.data.id };
  const method = methodOf(rpc.data.method);
  if (method !== undefined)
    return { envelope: named, method, params: rpc.data.params ?? {} };
  const cut = cutOperation(rpc.data.method);
  return {
    envelope: named,
    fault: cut ?? fault("MethodNotFoundError", `no method ${rpc.data.method}`),
  };
}

/** The GET operations' non-string fields, as the request schemas declare them. */
const NUMBERS: readonly string[] = ["pageSize", "historyLength"];
const BOOLEANS: readonly string[] = ["includeArtifacts"];

async function httpOperation(inbound: Inbound): Promise<Parsed> {
  const envelope: Envelope = { binding: "HTTP+JSON", id: undefined };
  const cut = cutPath(inbound.path, inbound.request.method);
  if (cut !== undefined) return { envelope, fault: cut };
  const found = inboundPath(inbound.path);
  if (found === null) return { envelope, fault: NOT_AN_OPERATION };
  const { method, id } = found;
  const verb = inbound.request.method;
  // The proto says GET for SubscribeToTask and the prose says POST, so both are accepted here.
  const allowed =
    verb === HTTP[method].verb ||
    (method === "SubscribeToTask" && verb === "POST");
  if (!allowed) return { envelope, fault: NOT_AN_OPERATION };
  const base = id === undefined ? {} : { id };
  if (HTTP[method].where === "body" && verb === "POST")
    return bodyParams(inbound, envelope, method, base);
  return { envelope, method, params: { ...fromQuery(inbound.url), ...base } };
}

async function bodyParams(
  inbound: Inbound,
  envelope: Envelope,
  method: Method,
  base: Readonly<Record<string, string>>,
): Promise<Parsed> {
  const body = parseJson(await inbound.request.text());
  if (!body.ok) return { envelope, fault: body.fault };
  const value = body.value;
  if (typeof value !== "object" || value === null || Array.isArray(value))
    return {
      envelope,
      fault: fault("InvalidRequestError", "the request body is a JSON object"),
    };
  return { envelope, method, params: { ...value, ...base } };
}

function fromQuery(url: URL): Readonly<Record<string, unknown>> {
  const params: Record<string, unknown> = {};
  for (const [key, value] of url.searchParams) {
    // Sent as a parameter as well as a header; it is the version check's, not the operation's.
    if (key === "A2A-Version") continue;
    params[key] = NUMBERS.includes(key)
      ? Number(value)
      : BOOLEANS.includes(key)
        ? value === "true"
        : value;
  }
  return params;
}
