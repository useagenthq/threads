import { z } from "zod";
import type { Opt, Strict } from "./zod";

// The A2A error set with its JSON-RPC code, HTTP status and `google.rpc.ErrorInfo` reason, taken
// from the pinned specification's error-code mapping table (spec/schema/a2a/README.md repeats it),
// never from memory. One table serves both bindings and both directions: we answer with it, and we
// read a peer's error by code, by status and by reason.

export const A2A_ERRORS = {
  JSONParseError: { code: -32700, status: 400, reason: "JSON_PARSE" },
  InvalidRequestError: { code: -32600, status: 400, reason: "INVALID_REQUEST" },
  MethodNotFoundError: {
    code: -32601,
    status: 404,
    reason: "METHOD_NOT_FOUND",
  },
  InvalidParamsError: { code: -32602, status: 400, reason: "INVALID_PARAMS" },
  InternalError: { code: -32603, status: 500, reason: "INTERNAL" },
  TaskNotFoundError: { code: -32001, status: 404, reason: "TASK_NOT_FOUND" },
  TaskNotCancelableError: {
    code: -32002,
    status: 400,
    reason: "TASK_NOT_CANCELABLE",
  },
  PushNotificationNotSupportedError: {
    code: -32003,
    status: 400,
    reason: "PUSH_NOTIFICATION_NOT_SUPPORTED",
  },
  UnsupportedOperationError: {
    code: -32004,
    status: 400,
    reason: "UNSUPPORTED_OPERATION",
  },
  ContentTypeNotSupportedError: {
    code: -32005,
    status: 400,
    reason: "CONTENT_TYPE_NOT_SUPPORTED",
  },
  InvalidAgentResponseError: {
    code: -32006,
    status: 500,
    reason: "INVALID_AGENT_RESPONSE",
  },
  ExtendedAgentCardNotConfiguredError: {
    code: -32007,
    status: 400,
    reason: "EXTENDED_AGENT_CARD_NOT_CONFIGURED",
  },
  ExtensionSupportRequiredError: {
    code: -32008,
    status: 400,
    reason: "EXTENSION_SUPPORT_REQUIRED",
  },
  VersionNotSupportedError: {
    code: -32009,
    status: 400,
    reason: "VERSION_NOT_SUPPORTED",
  },
} as const;

export type A2aErrorName = keyof typeof A2A_ERRORS;

/** Every name, so a lookup by code stays typed without a cast. */
export const A2A_ERROR_NAMES: readonly A2aErrorName[] = [
  "JSONParseError",
  "InvalidRequestError",
  "MethodNotFoundError",
  "InvalidParamsError",
  "InternalError",
  "TaskNotFoundError",
  "TaskNotCancelableError",
  "PushNotificationNotSupportedError",
  "UnsupportedOperationError",
  "ContentTypeNotSupportedError",
  "InvalidAgentResponseError",
  "ExtendedAgentCardNotConfiguredError",
  "ExtensionSupportRequiredError",
  "VersionNotSupportedError",
] as const satisfies readonly A2aErrorName[];

export type A2aFault = {
  readonly name: A2aErrorName;
  readonly message: string;
};

export function fault(name: A2aErrorName, message: string): A2aFault {
  return { name, message };
}

export function jsonRpcCode(name: A2aErrorName): number {
  return A2A_ERRORS[name].code;
}

export function httpStatus(name: A2aErrorName): number {
  return A2A_ERRORS[name].status;
}

/** The `google.rpc.ErrorInfo` reason this error is named by, which is stable across bindings. */
export function errorReason(name: A2aErrorName): string {
  return A2A_ERRORS[name].reason;
}

const BY_CODE: ReadonlyMap<number, A2aErrorName> = new Map(
  A2A_ERROR_NAMES.map((name) => [A2A_ERRORS[name].code, name]),
);

const BY_REASON: ReadonlyMap<string, A2aErrorName> = new Map(
  A2A_ERROR_NAMES.map((name) => [A2A_ERRORS[name].reason, name]),
);

/** A peer's JSON-RPC error code as its A2A name, or undefined for a code outside the table. */
export function errorByCode(code: number): A2aErrorName | undefined {
  return BY_CODE.get(code);
}

/** A peer's ErrorInfo reason as its A2A name, or undefined for a reason outside the table. */
export function errorByReason(reason: string): A2aErrorName | undefined {
  return BY_REASON.get(reason);
}

/**
 * The domain our own ErrorInfo carries. `domain` scopes `reason`, so it is the protocol's domain
 * rather than this host's: two A2A implementations must name the same error the same way. Inbound
 * we read any domain and check only the reason, because a partner's domain is theirs to choose.
 */
export const A2A_ERROR_DOMAIN = "a2a.dev";

/** The `@type` a `google.rpc.Status.details` entry carries when it is an ErrorInfo. */
export const ERROR_INFO_TYPE = "type.googleapis.com/google.rpc.ErrorInfo";

/**
 * `google.rpc.ErrorInfo` as one entry of `google.rpc.Status.details`. Not a message of the
 * vendored proto: the HTTP+JSON binding requires it, and the proto imports no google.rpc, so
 * spec/tools/check_a2a_pin.py lists it as an external message with that reason.
 *
 * `@type` is the `google.protobuf.Any` tag a details entry carries under the protobuf JSON
 * mapping, so it is part of the shape rather than something we add.
 */
export const ErrorInfo: Strict<{
  reason: z.ZodString;
  domain: z.ZodString;
  metadata: Opt<z.ZodRecord<z.ZodString, z.ZodString>>;
  "@type": Opt<z.ZodString>;
}> = z.strictObject({
  reason: z.string().min(1),
  domain: z.string().min(1),
  metadata: z.record(z.string(), z.string()).optional(),
  "@type": z.string().optional(),
});
export type ErrorInfo = z.infer<typeof ErrorInfo>;

/** Our own ErrorInfo for one fault, as it goes out on either binding. */
export function errorInfo(name: A2aErrorName): ErrorInfo {
  return {
    "@type": ERROR_INFO_TYPE,
    reason: errorReason(name),
    domain: A2A_ERROR_DOMAIN,
  };
}

export type FoundErrorInfo = {
  readonly info: ErrorInfo;
  /** The A2A error the reason names, or undefined for a reason outside our table. */
  readonly named: A2aErrorName | undefined;
};

/**
 * The ErrorInfo a value carries, and the A2A error its reason names. Both bindings put it
 * somewhere different — HTTP+JSON in the `details` list of a `google.rpc.Status`, JSON-RPC in the
 * error's own `data` — so a list is scanned and anything else is tried as the ErrorInfo itself.
 * `undefined` means there was none, which is the HTTP+JSON binding's one refusable case.
 */
export function errorInfoIn(value: unknown): FoundErrorInfo | undefined {
  const entries = Array.isArray(value) ? value : [value];
  for (const entry of entries) {
    const parsed = ErrorInfo.safeParse(entry);
    if (parsed.success)
      return { info: parsed.data, named: errorByReason(parsed.data.reason) };
  }
  return undefined;
}
