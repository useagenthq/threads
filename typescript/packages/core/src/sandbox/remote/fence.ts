import { AsyncLocalStorage } from "node:async_hooks";
import type { Fetch } from "../../model/transport";
import { err, type Result } from "../../result";
import type { SandboxContext, Stale } from "../protocol";

// The sandbox fence at a provider SDK's real transport (spec/api.json SandboxContext, // item 3). Every provider operation runs inside `within(context, ...)`, and the SDK's transport
// re-checks that context as each request leaves, after any SDK queueing or retry.

const current = new AsyncLocalStorage<SandboxContext>();

/** Thrown by a fenced transport before any byte leaves: the caller lost its authority. */
export class FenceRefused extends Error {
  override readonly name = "FenceRefused";
  constructor(readonly stale: Stale) {
    super(stale.message);
  }
}

/** Runs one provider operation under `context`; its transport requests fence against it. */
export function within<T>(
  context: SandboxContext,
  operation: () => Promise<T>,
): Promise<T> {
  return current.run(context, operation);
}

/**
 * The fence at a send point: passes only inside `within` and only while the context holds.
 * Outside an operation nothing may leave, so a stray SDK request is refused, never sent.
 */
export async function fenceHere(): Promise<void> {
  const context = current.getStore();
  if (context === undefined)
    throw new FenceRefused({
      code: "stale_epoch",
      message: "no threads sandbox operation is in progress",
    });
  const live = await context.fence();
  if (!live.ok) throw new FenceRefused(live.error);
}

/** Wraps the fetch a provider SDK sends through with the fence. */
export function sandboxFetch(inner: Fetch): Fetch {
  return async (input, init) => {
    await fenceHere();
    return inner(input, init);
  };
}

/** The refusal behind an SDK's wrapped transport error, if that is what stopped it. */
export function refusal(error: unknown): Stale | undefined {
  for (let e = error; e instanceof Error; e = e.cause)
    if (e instanceof FenceRefused) return e.stale;
  return undefined;
}

/**
 * One operation under its context: a fence refusal is its Stale value, any other throw is the
 * provider's failure, mapped by `failed`.
 */
export async function guarded<T, E>(
  context: SandboxContext,
  operation: () => Promise<Result<T, E>>,
  failed: (error: unknown) => E,
): Promise<Result<T, E | Stale>> {
  try {
    return await within(context, operation);
  } catch (error) {
    return err(refusal(error) ?? failed(error));
  }
}

/** A thrown value's message, for a typed failure. */
export function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
