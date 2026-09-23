import type { ZepClient } from "@getzep/zep-cloud";
import { type Fetch, sandboxFetch } from "@threads/core/adapter";

// The Zep SDK's transport hook is a whole `fetcher` (its default one reads the global fetch and
// retries inside). This is that hook over the fenced fetch: one request per call, no hidden
// retries, and the lease re-checked as the request leaves.

type FetchFunction = NonNullable<
  NonNullable<ConstructorParameters<typeof ZepClient>[0]>["fetcher"]
>;
type Args = Parameters<FetchFunction>[0];
type Reply = Awaited<ReturnType<FetchFunction>>;

type Supplied = NonNullable<Args["headers"]>[string];

async function header(value: Supplied): Promise<string | undefined> {
  const got = typeof value === "function" ? await value() : await value;
  return got ?? undefined;
}

function urlOf(args: Args): URL {
  const url = new URL(args.url);
  for (const [k, v] of Object.entries(args.queryParameters ?? {}))
    for (const item of [v].flat())
      if (item !== null)
        url.searchParams.append(
          k,
          typeof item === "string" ? item : JSON.stringify(item),
        );
  return url;
}

async function headersOf(args: Args): Promise<Headers> {
  const headers = new Headers();
  for (const [k, v] of Object.entries(args.headers ?? {})) {
    const value = await header(v);
    if (value !== undefined) headers.set(k, value);
  }
  if (args.body !== undefined)
    headers.set("content-type", args.contentType ?? "application/json");
  return headers;
}

function signalOf(args: Args): AbortSignal | undefined {
  const signals = [
    ...(args.abortSignal === undefined ? [] : [args.abortSignal]),
    ...(args.timeoutMs === undefined
      ? []
      : [AbortSignal.timeout(args.timeoutMs)]),
  ];
  return signals.length === 0 ? undefined : AbortSignal.any(signals);
}

async function request(args: Args): Promise<RequestInit & { url: URL }> {
  const signal = signalOf(args);
  return {
    url: urlOf(args),
    method: args.method,
    headers: await headersOf(args),
    ...(args.body === undefined ? {} : { body: JSON.stringify(args.body) }),
    ...(signal === undefined ? {} : { signal }),
  };
}

function raw(r: Response): Reply["rawResponse"] {
  return {
    headers: r.headers,
    redirected: r.redirected,
    status: r.status,
    statusText: r.statusText,
    type: r.type,
    url: r.url,
  };
}

async function reply(r: Response): Promise<Reply> {
  const text = await r.text();
  let body: unknown;
  try {
    body = text === "" ? undefined : JSON.parse(text);
  } catch {
    return {
      ok: false,
      error: { reason: "non-json", statusCode: r.status, rawBody: text },
      rawResponse: raw(r),
    };
  }
  return r.ok
    ? {
        ok: true,
        body,
        headers: Object.fromEntries(r.headers),
        rawResponse: raw(r),
      }
    : {
        ok: false,
        error: { reason: "status-code", statusCode: r.status, body },
        rawResponse: raw(r),
      };
}

const unsent = {
  headers: new Headers(),
  redirected: false,
  status: 0,
  statusText: "",
  type: "error",
  url: "",
} satisfies Reply["rawResponse"];

/**
 * The SDK's generic signature promises any body type; the body here is unknown JSON, and the
 * adapter parses every response it reads with its own schema (the SDK skips validation).
 */
export function fencedFetcher(inner: Fetch): FetchFunction;
export function fencedFetcher(inner: Fetch): (args: Args) => Promise<Reply> {
  const send = sandboxFetch(inner);
  return async (args) => {
    const { url, ...init } = await request(args);
    try {
      return await reply(await send(url, init));
    } catch (error) {
      if (error instanceof DOMException && error.name === "TimeoutError")
        return { ok: false, error: { reason: "timeout" }, rawResponse: unsent };
      // A fence refusal lands here too; the operation's scope has recorded it (dispatched()).
      return {
        ok: false,
        error: { reason: "unknown", errorMessage: String(error) },
        rawResponse: unsent,
      };
    }
  };
}
