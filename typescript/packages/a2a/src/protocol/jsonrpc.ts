import { JsonValue } from "@threads/core/adapter";
import { z } from "zod";
import {
  type A2aErrorName,
  type A2aFault,
  errorByCode,
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

export function rpcFault(id: RpcId | undefined, f: A2aFault): string {
  return JSON.stringify({
    jsonrpc: "2.0",
    id: id ?? null,
    error: { code: jsonRpcCode(f.name), message: `${f.name}: ${f.message}` },
  });
}

/** A peer's JSON-RPC answer: its result, or the A2A error it names. */
export function rpcOutcome(
  body: unknown,
): { readonly result: unknown } | { readonly fault: A2aFault } {
  const parsed = RpcResponse.safeParse(body);
  if (!parsed.success)
    return {
      fault: fault("InvalidAgentResponseError", "not a JSON-RPC 2.0 response"),
    };
  const { result, error } = parsed.data;
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

/** A peer's error as one of ours. A code outside the table is an InternalError we record. */
function faultOf(error: RpcError): A2aFault {
  const named: A2aErrorName | undefined = errorByCode(error.code);
  return fault(
    named ?? "InternalError",
    named === undefined
      ? `the peer answered JSON-RPC ${error.code}: ${error.message}`
      : error.message,
  );
}
