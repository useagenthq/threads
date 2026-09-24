import type { SyncError } from "@threads/core";
import { err, ok, type Result } from "@threads/core/host";
import type { Config } from "./env";

// One OTLP/HTTP JSON POST. The collector's answer decides the error: a network error, a timeout,
// 408, 429 or 5xx may pass (collector_unavailable); any other 4xx won't (collector_rejected).
// Header values are credentials: they never appear in a message.

const BODY_LIMIT = 500;

async function gzip(text: string): Promise<ArrayBuffer> {
  const stream = new Blob([text])
    .stream()
    .pipeThrough(new CompressionStream("gzip"));
  return await new Response(stream).arrayBuffer();
}

function unavailable(config: Config, why: string, status?: number): SyncError {
  const message = `the collector at ${config.endpoint} is unavailable: ${why}`;
  return status === undefined
    ? { code: "collector_unavailable", message }
    : { code: "collector_unavailable", status, message };
}

export async function post(
  config: Config,
  body: string,
): Promise<Result<void, SyncError>> {
  const gzipped = config.compression === "gzip";
  let response: Response;
  try {
    response = await fetch(config.endpoint, {
      method: "POST",
      headers: {
        ...config.headers,
        "content-type": "application/json",
        ...(gzipped ? { "content-encoding": "gzip" } : {}),
      },
      body: gzipped ? await gzip(body) : body,
      signal: AbortSignal.timeout(config.timeoutMs),
    });
  } catch (error) {
    const why =
      error instanceof Error && error.name === "TimeoutError"
        ? "timed out"
        : "no answer";
    return err(unavailable(config, why));
  }
  const text = await response.text();
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
    message: `the collector at ${config.endpoint} rejected the spans (HTTP ${status}): ${start}`,
  });
}
