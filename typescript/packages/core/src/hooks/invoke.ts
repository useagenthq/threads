import { checkReturn } from "./check";
import type { HookArgs, HookName, HookReturns, LoopExtension } from "./types";

// every hook call is awaited and time-bounded. A throw, a timeout or a value
// the hook may not return is `failed`; what that means (deny, or nothing) is the caller's
// class rule.

export type Outcome<K extends HookName> =
  | { readonly kind: "ok"; readonly value: HookReturns[K] }
  | { readonly kind: "failed"; readonly reason: string };

const failed = (reason: string): { kind: "failed"; reason: string } => ({
  kind: "failed",
  reason,
});

/** Runs one extension's hook, or returns undefined when the extension doesn't define it. */
export async function invoke<K extends HookName>(
  ext: LoopExtension,
  hook: K,
  args: HookArgs[K],
  callId?: string,
): Promise<Outcome<K> | undefined> {
  const fn = ext.hooks[hook];
  if (fn === undefined) return undefined;
  const controller = new AbortController();
  const ctx = {
    signal: controller.signal,
    ...(callId === undefined ? {} : { callId }),
  };
  const settled = (async (): Promise<Outcome<K>> => {
    try {
      const checked = checkReturn(hook, await fn(args, ctx));
      return checked === undefined
        ? failed("returned a value this hook may not return")
        : { kind: "ok", value: checked.value };
    } catch (error) {
      return failed(
        `threw: ${error instanceof Error ? error.message : String(error)}`,
      );
    }
  })();
  const deadline = Promise.withResolvers<Outcome<K>>();
  const timer = setTimeout(() => {
    controller.abort();
    deadline.resolve(failed(`timed out after ${ext.timeoutMs} ms`));
  }, ext.timeoutMs);
  try {
    return await Promise.race([settled, deadline.promise]);
  } finally {
    clearTimeout(timer);
  }
}
