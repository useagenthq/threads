import { lookup } from "node:dns/promises";
import http from "node:http";
import https from "node:https";
import { isIP, type LookupFunction } from "node:net";
import { isPublicAddress } from "./ssrf";

// The host web transport for web_fetch and the search backends: every address
// a host resolves to must be public, and the connection goes to the address that was checked,
// so a second DNS answer can't swap in a private one. TLS still verifies the URL's host name.

export type Sent = {
  readonly method?: "GET" | "POST";
  readonly headers: Readonly<Record<string, string>>;
  readonly body?: string;
  readonly signal: AbortSignal;
};

/** The host's network, injectable so tests never touch it. Redirects are never followed. */
export type WebTransport = {
  readonly resolve: (host: string) => Promise<readonly string[]>;
  /** One request to `url`, connected to `address`, which `vet` checked. */
  readonly fetch: (
    url: string,
    address: string,
    init: Sent,
  ) => Promise<Response>;
};

/** The host of a URL, with the brackets an IPv6 literal is written in taken off. */
export function hostOf(url: URL): string {
  return url.hostname.replace(/^\[|\]$/g, "");
}

/**
 * Why a URL could never be fetched, or undefined when it might be — decided without resolving
 * anything: a scheme we fetch, no credentials in the URL, and a host that is either a name or a
 * public IP literal. A name passes here and is decided by `vet`, which resolves it; that is the
 * only moment its addresses are known, so a name must not be judged before then.
 *
 * Choosing between URLs that are not being fetched yet (an agent card's interfaces) needs exactly
 * this, and needs it without a lookup, so the one list of non-public ranges is read from here too.
 */
export function unfetchable(url: URL): string | undefined {
  if (url.protocol !== "https:" && url.protocol !== "http:")
    return `only http and https URLs are fetched, not ${url.protocol}`;
  if (url.username !== "" || url.password !== "")
    return "a URL with credentials is not fetched";
  const host = hostOf(url);
  return isIP(host) !== 0 && !isPublicAddress(host)
    ? `${host} is a non-public address`
    : undefined;
}

/** The checked address to connect to, or why the URL is refused. */
export async function vet(
  url: URL,
  transport: WebTransport,
): Promise<{ readonly address: string } | { readonly denied: string }> {
  const why = unfetchable(url);
  if (why !== undefined) return { denied: why };
  const host = hostOf(url);
  let addresses: readonly string[];
  try {
    addresses = isIP(host) === 0 ? await transport.resolve(host) : [host];
  } catch {
    return { denied: `${host} does not resolve` };
  }
  const [first] = addresses;
  if (first === undefined) return { denied: `${host} does not resolve` };
  const blocked = addresses.find((a) => !isPublicAddress(a));
  return blocked === undefined
    ? { address: first }
    : { denied: `${host} resolves to a non-public address (${blocked})` };
}

/** Resolves every host to the one checked address. */
function pinned(address: string): LookupFunction {
  const family = isIP(address);
  return (_host, options, done) =>
    options.all === true
      ? done(null, [{ address, family }])
      : done(null, address, family);
}

function send(url: string, address: string, init: Sent): Promise<Response> {
  const { promise, resolve, reject } = Promise.withResolvers<Response>();
  const target = new URL(url);
  const client = target.protocol === "https:" ? https : http;
  const req = client.request(
    target,
    {
      method: init.method ?? "GET",
      headers: init.headers,
      signal: init.signal,
      lookup: pinned(address),
    },
    (res) => {
      const headers = new Headers();
      for (const [name, value] of Object.entries(res.headers))
        for (const v of [value ?? []].flat()) headers.append(name, v);
      const status = res.statusCode ?? 502;
      const body = new ReadableStream<Uint8Array>({
        start: (controller) => {
          res.on("data", (chunk: Buffer) =>
            controller.enqueue(new Uint8Array(chunk)),
          );
          res.on("end", () => controller.close());
          res.on("error", (error) => controller.error(error));
        },
        cancel: () => {
          res.destroy();
        },
      });
      const empty = status === 204 || status === 304;
      resolve(new Response(empty ? null : body, { status, headers }));
    },
  );
  req.on("error", reject);
  req.end(init.body);
  return promise;
}

export const liveTransport: WebTransport = {
  resolve: async (host) =>
    (await lookup(host, { all: true, verbatim: true })).map((a) => a.address),
  fetch: send,
};
