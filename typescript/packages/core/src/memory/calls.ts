import type { ToolContext, ToolRun } from "../loop/types";
import type { Result } from "../result";
import { dispatched } from "../sandbox/remote/fence";
import type { ProviderError } from "./protocol";

// One provider call from a memory or knowledge tool, under the tool's lease fence at the
// adapter's real transport, bounded in time, and what its failure amounts to.

/** A provider that doesn't answer in time is a typed timeout, never a hung run. */
const TIMEOUT_MS = 10_000;

export type Called<T> =
  | { readonly ok: true; readonly value: T }
  | {
      readonly ok: false;
      /** A refusal at the fence before any request left: provably nothing was sent. */
      readonly notSent: boolean;
      /** The provider's typed error; undefined when it threw instead. */
      readonly error: ProviderError | undefined;
    };

type Reply<T> = Result<T, ProviderError>;

async function timed<T>(body: () => Promise<Reply<T>>): Promise<Reply<T>> {
  const { promise, resolve } = Promise.withResolvers<Reply<T>>();
  const timer = setTimeout(
    () =>
      resolve({
        ok: false,
        error: {
          code: "timeout",
          message: `the provider didn't answer in ${TIMEOUT_MS / 1000}s`,
        },
      }),
    TIMEOUT_MS,
  );
  try {
    return await Promise.race([body(), promise]);
  } finally {
    clearTimeout(timer);
  }
}

export async function called<T>(
  ctx: ToolContext,
  body: () => Promise<Reply<T>>,
): Promise<Called<T>> {
  const d = await dispatched(ctx, () => timed(body));
  if (d.outcome.ok && d.outcome.value.ok)
    return { ok: true, value: d.outcome.value.value };
  return {
    ok: false,
    notSent: d.refused && !d.sent,
    error:
      d.outcome.ok && !d.outcome.value.ok ? d.outcome.value.error : undefined,
  };
}

export function failedRun(error: ProviderError): ToolRun {
  return {
    kind: "done",
    output: `${error.code}: ${error.message}`,
    isError: true,
  };
}

/** A read changes nothing: any failure is a recorded, typed error and the run goes on (F3.6). */
export function readFailure(
  c: Extract<Called<unknown>, { ok: false }>,
): ToolRun {
  if (c.notSent) return { kind: "not_sent" };
  return failedRun(
    c.error ?? { code: "unavailable", message: "the provider failed" },
  );
}

/** A write after dispatch is uncertain unless the provider answered with a typed refusal (C3). */
export function writeRun<T>(
  c: Called<T>,
  output: (value: T) => string,
): ToolRun {
  if (c.ok) return { kind: "done", output: output(c.value), isError: false };
  if (c.notSent) return { kind: "not_sent" };
  switch (c.error?.code) {
    case "invalid":
    case "scope_violation":
      return failedRun(c.error);
    case "timeout":
      return { kind: "unknown", reason: "timeout" };
    default:
      return { kind: "unknown", reason: "transport_error" };
  }
}
