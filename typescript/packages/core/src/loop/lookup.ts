import { AssertionError } from "node:assert";
import { StoreError } from "../store/driver";

/**
 * An adapter's lookup, where throwing answers unknown: recovery then settles or parks under its
 * own rules, and the run is never retried for a provider's error. A store error still throws,
 * and so does a failed assertion: a bug surfaces as a bug (Python: `AssertionError`).
 */
export async function lookedUp<T>(
  lookup: () => Promise<T>,
  unknown: (reason: string) => T,
): Promise<T> {
  try {
    return await lookup();
  } catch (error) {
    if (error instanceof StoreError || error instanceof AssertionError)
      throw error;
    const name = error instanceof Error ? error.name : "error";
    return unknown(`the lookup failed: ${name}`);
  }
}
