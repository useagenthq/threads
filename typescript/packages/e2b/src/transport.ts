import { AsyncLocalStorage } from "node:async_hooks";
import { type Fetch, sandboxFetch } from "@threads/core/adapter";

// The E2B SDK takes no fetch. Outside Node it sends every request (control plane and envd)
// through `globalThis.fetch`, looked up at each call, so that is its real transport: requests it
// makes inside `through` go to the adapter's fetch behind the sandbox fence; any other fetch in
// the process passes straight on. On Node the SDK binds undici instead, which nothing can wrap,
// so e2b() refuses that route at setup (index.ts).

const route = new AsyncLocalStorage<Fetch>();
const base = globalThis.fetch;
let installed = false;

/** The process's own fetch, as it was before the route was installed. */
export const networkFetch: Fetch = (input, init) => base(input, init);

function install(): void {
  if (installed) return;
  installed = true;
  const routed = async (
    input: string | URL | Request,
    init?: RequestInit,
  ): Promise<Response> => {
    const inner = route.getStore();
    return inner === undefined
      ? base(input, init)
      : sandboxFetch(inner)(input, init);
  };
  globalThis.fetch = Object.assign(routed, { preconnect: base.preconnect });
}

/** Whether the SDK's transport can be fenced in this runtime (it binds undici on Node). */
export function fenceable(runtime: object = globalThis): boolean {
  return "Bun" in runtime || "Deno" in runtime;
}

/** Runs SDK calls whose requests leave through `inner`, behind the current sandbox fence. */
export function through<T>(inner: Fetch, call: () => Promise<T>): Promise<T> {
  install();
  return route.run(inner, call);
}
