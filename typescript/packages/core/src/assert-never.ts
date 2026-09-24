import { AssertionError } from "node:assert";

/**
 * Ends every switch over a union: a new variant fails to compile here instead of falling through.
 * At run time it is a broken invariant, an AssertionError, as Python's `assert_never` raises: a
 * bug surfaces as a bug wherever it is caught (a lookup, a stream).
 */
export function assertNever(value: never): never {
  throw new AssertionError({
    message: `unexpected variant: ${JSON.stringify(value)}`,
  });
}
