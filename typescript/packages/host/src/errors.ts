// Every non-2xx JSON response of the host API is an ErrorBody (host-api.v1.schema.json) whose
// code is one of its route's x-error-codes (openapi.json). Status: 400 invalid_request,
// 401 unauthenticated and unverified, 403 forbidden, 404 not_found, 409 any domain code.

export type Failure = { readonly code: string; readonly message: string };

const STATUS: Readonly<Record<string, number>> = {
  invalid_request: 400,
  unauthenticated: 401,
  unverified: 401,
  forbidden: 403,
  not_found: 404,
};

export function statusOf(code: string): number {
  return STATUS[code] ?? 409;
}

export function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

export function failure(code: string, message: string): Response {
  return json(statusOf(code), { error: { code, message } });
}

/**
 * A failure as its route answers it: a code outside the route's x-error-codes is a bug in the
 * host, never a new wire code, so it throws.
 */
export function routeFailure(
  allowed: readonly string[],
  error: Failure,
): Response {
  if (!allowed.includes(error.code))
    throw new Error(`${error.code} is not a failure of this route`);
  return failure(error.code, error.message);
}
