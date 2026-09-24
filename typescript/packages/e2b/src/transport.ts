import { type Fetch, sandboxFetch } from "@threads/core/adapter";

// Every E2B request, control plane and envd alike, leaves through the adapter's fetch (e2b's
// `fetch` option, the runtime's own by default) behind the sandbox fence at its real send
// point. Each is one attempt with redirects left unfollowed: a followed redirect would be a
// second send no fence saw. Nothing here retries; a lost answer is the ledger's to settle.

export type Send = (url: string, init: RequestInit) => Promise<Response>;

/** How long a REST, file or signal request may take: the e2b SDKs' request timeout. */
const REQUEST_MS = 60_000;

export function sender(inner: Fetch): Send {
  const fenced = sandboxFetch(inner);
  return (url, init) => fenced(url, { ...init, redirect: "manual" });
}

/** A request that ends on its own: everything but an exec's output stream. */
export function bounded(init: RequestInit): RequestInit {
  return { ...init, signal: AbortSignal.timeout(REQUEST_MS) };
}
