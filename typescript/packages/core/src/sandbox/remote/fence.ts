import { AsyncLocalStorage } from "node:async_hooks";
import type { Fetch } from "../../model/transport";
import { err, ok, type Result } from "../../result";
import type { SandboxContext, Stale } from "../protocol";

// The sandbox fence at a provider SDK's real transport (spec/api.json SandboxContext, // item 3). Every provider operation runs inside `within(context, ...)`, and the SDK's transport
// re-checks that context as each request leaves, after any SDK queueing or retry. A refusal is
// also recorded on the operation, because an SDK may rethrow it as its own error without cause.

/** What an operation is fenced by: a sandbox context, or any lease-bound fence (a tool's). */
export type Fence = Pick<SandboxContext, "fence">;

type Scope = { readonly context: Fence; refused?: Stale; sent: boolean };
const current = new AsyncLocalStorage<Scope>();

/** Thrown by a fenced transport before any byte leaves: the caller lost its authority. */
export class FenceRefused extends Error {
  override readonly name = "FenceRefused";
  constructor(readonly stale: Stale) {
    super(stale.message);
  }
}

/**
 * Runs one provider operation under `context`, whose transport requests fence against it. A
 * throw is the refusal that stopped it (however the SDK wrapped it), or the provider's error.
 */
export async function within<T>(
  context: Fence,
  operation: () => Promise<T>,
): Promise<Result<T, { readonly stale?: Stale; readonly error: unknown }>> {
  const scope: Scope = { context, sent: false };
  try {
    return ok(await current.run(scope, operation));
  } catch (error) {
    const stale = scope.refused ?? causedBy(error);
    return err(stale === undefined ? { error } : { stale, error });
  }
}

/**
 * The fence at a send point: passes only inside `within` and only while the context holds.
 * Outside an operation nothing may leave, so a stray SDK request is refused, never sent.
 */
export async function fenceHere(): Promise<void> {
  const scope = current.getStore();
  if (scope === undefined)
    throw new FenceRefused({
      code: "stale_epoch",
      message: "no threads sandbox operation is in progress",
    });
  const live = await scope.context.fence();
  if (live.ok) {
    scope.sent = true;
    return;
  }
  scope.refused = live.error;
  throw new FenceRefused(live.error);
}

/**
 * One operation of a provider that answers with values (memory, MCP): its outcome, whether any
 * request passed the fence (`sent`), and whether one was refused. Only `refused && !sent`
 * proves nothing left; anything else after a failure is uncertain.
 */
export type Dispatched<T> = {
  readonly outcome: Result<T, unknown>;
  readonly sent: boolean;
  readonly refused: boolean;
};

export async function dispatched<T>(
  context: Fence,
  operation: () => Promise<T>,
): Promise<Dispatched<T>> {
  const scope: Scope = { context, sent: false };
  try {
    const value = await current.run(scope, operation);
    return {
      outcome: ok(value),
      sent: scope.sent,
      refused: scope.refused !== undefined,
    };
  } catch (error) {
    const refused = scope.refused ?? causedBy(error);
    return {
      outcome: err(error),
      sent: scope.sent,
      refused: refused !== undefined,
    };
  }
}

/** Wraps the fetch a provider SDK sends through with the fence. */
export function sandboxFetch(inner: Fetch): Fetch {
  return async (input, init) => {
    await fenceHere();
    return inner(input, init);
  };
}

function causedBy(error: unknown): Stale | undefined {
  for (let e = error; e instanceof Error; e = e.cause)
    if (e instanceof FenceRefused) return e.stale;
  return undefined;
}

/** Whether the operation in progress was refused: nothing more of it may be dispatched. */
export function refusedHere(): boolean {
  return current.getStore()?.refused !== undefined;
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
  const done = await within(context, operation);
  if (done.ok) return done.value;
  return err(done.error.stale ?? failed(done.error.error));
}

/** A thrown value's message, for a typed failure. */
export function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
