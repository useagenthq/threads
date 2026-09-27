import { JsonValue } from "@threads/core/adapter";
import { z } from "zod";
import {
  type A2aErrorName,
  type A2aFault,
  errorByCode,
  errorInfo,
  errorInfoIn,
  fault,
  jsonRpcCode,
} from "./errors";
import type { Opt, Strict } from "./zod";

// The JSON-RPC 2.0 binding's envelope. PascalCase method names matching the gRPC service, an id
// that is a string, a number or null, and `params` that is the operation's own request message.
// Streaming operations answer with SSE whose items are `{jsonrpc, id, result: <StreamResponse>}`.

export const METHODS = [
  "SendMessage",
  "SendStreamingMessage",
  "GetTask",
  "ListTasks",
  "CancelTask",
  "SubscribeToTask",
] as const;

export type Method = (typeof METHODS)[number];

/** A method name we recognise, or undefined: an unknown one is MethodNotFoundError. */
export function methodOf(name: string): Method | undefined {
  return METHODS.find((m) => m === name);
}

/** The id echoed back. JSON-RPC allows a string, a number or null; anything else is invalid. */
export const RpcId: z.ZodUnion<[z.ZodString, z.ZodNumber, z.ZodNull]> = z.union(
  [z.string(), z.number(), z.null()],
);
export type RpcId = z.infer<typeof RpcId>;

export const RpcRequest: Strict<{
  jsonrpc: z.ZodLiteral<"2.0">;
  id: Opt<typeof RpcId>;
  method: z.ZodString;
  params: Opt<typeof JsonValue>;
}> = z.strictObject({
  jsonrpc: z.literal("2.0"),
  id: RpcId.optional(),
  method: z.string().min(1),
  params: JsonValue.optional(),
});
export type RpcRequest = z.infer<typeof RpcRequest>;

export const RpcError: Strict<{
  code: z.ZodNumber;
  message: z.ZodString;
  data: Opt<typeof JsonValue>;
}> = z.strictObject({
  code: z.number(),
  message: z.string(),
  data: JsonValue.optional(),
});
export type RpcError = z.infer<typeof RpcError>;

export const RpcResponse: Strict<{
  jsonrpc: z.ZodLiteral<"2.0">;
  id: Opt<typeof RpcId>;
  result: Opt<typeof JsonValue>;
  error: Opt<typeof RpcError>;
}> = z.strictObject({
  jsonrpc: z.literal("2.0"),
  id: RpcId.optional(),
  result: JsonValue.optional(),
  error: RpcError.optional(),
});
export type RpcResponse = z.infer<typeof RpcResponse>;

export function rpcResult(id: RpcId | undefined, result: unknown): string {
  return JSON.stringify({ jsonrpc: "2.0", id: id ?? null, result });
}

/**
 * A refusal in the envelope. `data` carries the ErrorInfo, which the binding says a JSON-RPC error
 * SHOULD have: the code already names the error, so this is the same name said the way the
 * HTTP+JSON binding says it, and a peer that reads only one of the two still agrees with us.
 */
export function rpcFault(id: RpcId | undefined, f: A2aFault): string {
  return JSON.stringify({
    jsonrpc: "2.0",
    id: id ?? null,
    error: {
      code: jsonRpcCode(f.name),
      message: `${f.name}: ${f.message}`,
      data: errorInfo(f.name),
    },
  });
}

/**
 * A peer's JSON-RPC answer: its result, or the A2A error it names. `sent` is the id we put on the
 * request, and an answer is only ours if it carries that id back: a response is a partner's bytes,
 * so it proves it answers us before we read anything out of it. `undefined` means we sent no id
 * (the HTTP+JSON binding), so there is nothing to correlate.
 */
export function rpcOutcome(
  body: unknown,
  sent: string | undefined,
): { readonly result: unknown } | { readonly fault: A2aFault } {
  const parsed = RpcResponse.safeParse(body);
  if (!parsed.success)
    return {
      fault: fault("InvalidAgentResponseError", "not a JSON-RPC 2.0 response"),
    };
  const { id, result, error } = parsed.data;
  const stray = uncorrelated(sent, id, error !== undefined);
  if (stray !== undefined) return { fault: stray };
  if (error !== undefined) return { fault: faultOf(error) };
  if (result === undefined)
    return {
      fault: fault(
        "InvalidAgentResponseError",
        "a JSON-RPC response carries a result or an error",
      ),
    };
  return { result };
}

/**
 * Why an answer is not the answer to our request, or undefined when it is. JSON-RPC 2.0 requires a
 * null id on an error whose request id could not be determined, so a null id on an *error* is
 * correlated: it cannot forge a result, and refusing it would throw away the peer's real error.
 */
function uncorrelated(
  sent: string | undefined,
  got: RpcId | undefined,
  isError: boolean,
): A2aFault | undefined {
  if (sent === undefined || got === sent) return undefined;
  if (isError && got === null) return undefined;
  return fault(
    "InvalidAgentResponseError",
    `the answer carries id ${JSON.stringify(got ?? null)}, not the ${JSON.stringify(sent)} we sent`,
  );
}

/**
 * A peer's error as one of ours. A code outside the table is an InternalError we record. An
 * ErrorInfo in `data` is checked when there is one and left alone when there is not, because the
 * binding only says a JSON-RPC error SHOULD carry it; one that disagrees with the code is a
 * response we cannot read, not an error we can act on.
 */
function faultOf(error: RpcError): A2aFault {
  const named: A2aErrorName | undefined = errorByCode(error.code);
  const found = errorInfoIn(error.data);
  if (found !== undefined && named !== undefined && found.named !== named)
    return fault(
      "InvalidAgentResponseError",
      `the peer answered JSON-RPC ${error.code} with ErrorInfo reason ${found.info.reason}`,
    );
  return fault(
    named ?? "InternalError",
    named === undefined
      ? `the peer answered JSON-RPC ${error.code}: ${error.message}`
      : error.message,
  );
}
