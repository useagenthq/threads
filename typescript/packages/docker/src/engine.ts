import { type Fetch, sandboxFetch } from "@threads/core/adapter";
import { z } from "zod";
import { excerpt, parsed, unavailable } from "./wire";

// The Docker Engine HTTP API, spoken directly over the socket with no SDK. The API version is
// pinned in every path, the fence is checked at the real send point (sandboxFetch), each
// request is one attempt and no redirect is followed: a followed redirect would be a second
// send no fence saw. Nothing here retries; a lost answer is the ledger's to settle.

/** The Engine API this adapter speaks. A daemon below it answers the negotiation error. */
export const API = "v1.44";

export type Body =
  | { readonly json: unknown }
  | { readonly tar: Uint8Array }
  | undefined;

/** What an inspect establishes about a container: it is the applied config, not the request. */
export type Inspected = {
  readonly running: boolean;
  readonly nanoCpus: number;
  readonly memory: number;
  readonly pidsLimit: number;
};

const InspectedWire = z.object({
  State: z.object({ Running: z.boolean() }),
  HostConfig: z.object({
    NanoCpus: z.number().nullish(),
    Memory: z.number().nullish(),
    PidsLimit: z.number().nullish(),
  }),
});

export type Engine = {
  /** One fenced request. Every status is returned; only a transport failure throws. */
  readonly send: (
    method: string,
    path: string,
    body?: Body,
  ) => Promise<Response>;
  /** The reply of a request that must succeed, parsed as `schema`. */
  readonly json: <T>(
    schema: z.ZodType<T>,
    what: string,
    method: string,
    path: string,
    body?: Body,
  ) => Promise<T>;
  /** The container's applied state, or undefined when the daemon has no such container. */
  readonly inspect: (name: string) => Promise<Inspected | undefined>;
};

/**
 * A 400 that names the API version is a daemon too old to speak v1.44, not a bad request.
 * Docker answers version negotiation with a 400 whose body mentions the version.
 */
function tooOld(status: number, body: string): boolean {
  return (
    status === 400 && /api version|client version|too (new|old)/i.test(body)
  );
}

export async function failure(res: Response, what: string): Promise<Error> {
  const body = await excerpt(res);
  if (tooOld(res.status, body))
    return unavailable(
      "Docker Engine API 1.44 or newer is needed (Docker 25+)",
    );
  return unavailable(`Docker ${what} failed with ${res.status}: ${body}`);
}

export function engine(inner: Fetch): Engine {
  const fenced = sandboxFetch(inner);
  const send: Engine["send"] = (method, path, body) => {
    const init: RequestInit = { method, redirect: "manual" };
    if (body !== undefined && "json" in body)
      Object.assign(init, {
        headers: { "content-type": "application/json" },
        body: JSON.stringify(body.json),
      });
    if (body !== undefined && "tar" in body)
      Object.assign(init, {
        headers: { "content-type": "application/x-tar" },
        body: new Blob([body.tar.slice()]),
      });
    return fenced(`http://docker/${API}${path}`, init);
  };

  const json: Engine["json"] = async (schema, what, method, path, body) => {
    const res = await send(method, path, body);
    if (!res.ok) throw await failure(res, what);
    return parsed(schema, await res.text(), what);
  };

  return {
    send,
    json,
    inspect: async (name) => {
      const res = await send(
        "GET",
        `/containers/${encodeURIComponent(name)}/json`,
      );
      if (res.status === 404) {
        await res.text();
        return undefined;
      }
      if (!res.ok) throw await failure(res, "a container inspect");
      const wire = parsed(
        InspectedWire,
        await res.text(),
        "a container inspect",
      );
      return {
        running: wire.State.Running,
        nanoCpus: wire.HostConfig.NanoCpus ?? 0,
        memory: wire.HostConfig.Memory ?? 0,
        pidsLimit: wire.HostConfig.PidsLimit ?? 0,
      };
    },
  };
}
