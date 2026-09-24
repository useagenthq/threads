import type { SyncError } from "@threads/core";
import { err, ok, type Result } from "@threads/core/host";
import type { Config } from "./env";

// One OTLP/HTTP JSON POST. The collector's answer decides the error: a network error, a timeout,
// 408, 429 or 5xx may pass (collector_unavailable); any other 4xx won't (collector_rejected).
// Credentials never appear in a message: not the headers, and not a URL's userinfo or query
// string, where some vendors and proxies take a key.

const BODY_LIMIT = 500;

async function gzip(text: string): Promise<ArrayBuffer> {
  const stream = new Blob([text])
    .stream()
    .pipeThrough(new CompressionStream("gzip"));
  return await new Response(stream).arrayBuffer();
}

/** The endpoint as messages show it: origin and path only. */
export function shown(endpoint: string): string {
  try {
    const url = new URL(endpoint);
    return `${url.origin}${url.pathname}`;
  } catch {
    return "the configured endpoint";
  }
}

function unavailable(config: Config, why: string, status?: number): SyncError {
  const message = `the collector at ${shown(config.endpoint)} is unavailable: ${why}`;
  return status === undefined
    ? { code: "collector_unavailable", message }
    : { code: "collector_unavailable", status, message };
}

function failure(error: unknown): string {
  if (!(error instanceof Error)) return "no answer";
  if (error.name === "TimeoutError") return "timed out";
  return error.name === "AbortError" ? "the host stopped" : "no answer";
}

/** Posts one body; `stop` (the host's stop()) aborts it as the timeout does. */
export async function post(
  config: Config,
  body: string,
  stop?: AbortSignal,
): Promise<Result<void, SyncError>> {
  const gzipped = config.compression === "gzip";
  const timeout = AbortSignal.timeout(config.timeoutMs);
  let response: Response;
  let text: string;
  try {
    response = await fetch(config.endpoint, {
      method: "POST",
      headers: {
        ...config.headers,
        "content-type": "application/json",
        ...(gzipped ? { "content-encoding": "gzip" } : {}),
      },
      body: gzipped ? await gzip(body) : body,
      signal: stop === undefined ? timeout : AbortSignal.any([stop, timeout]),
    });
    text = await response.text();
  } catch (error) {
    return err(unavailable(config, failure(error)));
  }
  const status = response.status;
  if (status >= 200 && status < 300) return ok(undefined);
  if (status === 408 || status === 429 || status >= 500)
    return err(unavailable(config, `HTTP ${status}`, status));
  const start = new TextDecoder().decode(
    new TextEncoder().encode(text).slice(0, BODY_LIMIT),
  );
  return err({
    code: "collector_rejected",
    status,
    message: `the collector at ${shown(config.endpoint)} rejected the spans (HTTP ${status}): ${start}`,
  });
}
