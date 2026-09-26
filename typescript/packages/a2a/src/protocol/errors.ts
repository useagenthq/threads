// The A2A error set with its JSON-RPC code and HTTP status, taken from the pinned specification's
// error-code mapping table (spec/schema/a2a/README.md repeats it), never from memory. One table
// serves both bindings and both directions: we answer with it, and we read a peer's error by code.

export const A2A_ERRORS = {
  JSONParseError: { code: -32700, status: 400 },
  InvalidRequestError: { code: -32600, status: 400 },
  MethodNotFoundError: { code: -32601, status: 404 },
  InvalidParamsError: { code: -32602, status: 400 },
  InternalError: { code: -32603, status: 500 },
  TaskNotFoundError: { code: -32001, status: 404 },
  TaskNotCancelableError: { code: -32002, status: 400 },
  PushNotificationNotSupportedError: { code: -32003, status: 400 },
  UnsupportedOperationError: { code: -32004, status: 400 },
  ContentTypeNotSupportedError: { code: -32005, status: 400 },
  InvalidAgentResponseError: { code: -32006, status: 500 },
  ExtendedAgentCardNotConfiguredError: { code: -32007, status: 400 },
  ExtensionSupportRequiredError: { code: -32008, status: 400 },
  VersionNotSupportedError: { code: -32009, status: 400 },
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

const BY_CODE: ReadonlyMap<number, A2aErrorName> = new Map(
  A2A_ERROR_NAMES.map((name) => [A2A_ERRORS[name].code, name]),
);

/** A peer's JSON-RPC error code as its A2A name, or undefined for a code outside the table. */
export function errorByCode(code: number): A2aErrorName | undefined {
  return BY_CODE.get(code);
}
