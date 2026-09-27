import {
  A2A_VERSION,
  errorByCode,
  type Task,
  VERSION_HEADER,
} from "@threads/a2a/protocol";
import { type Agent, type Store, sqlite } from "@threads/core";
import type { Principal } from "@threads/core/host";
import { z } from "zod";
import type { A2aOptions } from "../../src";
import { type Host, host } from "../../src";
import { authenticate } from "../kit";

// The exposed side's kit: a host that has been through ready(), and one call helper per binding.
// Every assertion here goes through h.fetch, so a test exercises the routes a partner would.

export type Options = {
  readonly as?: Principal;
  readonly body?: unknown;
  /** Exact bytes, sent as they are: for a payload that must not be valid JSON. */
  readonly rawBody?: string;
  /** Null sends no A2A-Version header at all; a string sends that one. Default: 1.0. */
  readonly version?: string | null;
  /** Sends the version as the query parameter the spec also allows instead of the header. */
  readonly versionQuery?: string;
  readonly headers?: Readonly<Record<string, string>>;
};

export type Served = {
  readonly host: Host;
  readonly store: Store;
  /** POST /a2a/{agent}: the JSON-RPC binding. */
  readonly rpc: (
    method: string,
    params: unknown,
    options?: Options,
  ) => Promise<Response>;
  /** The HTTP+JSON binding, at a path under /a2a/{agent}. */
  readonly http: (
    verb: string,
    path: string,
    options?: Options,
  ) => Promise<Response>;
  /** Any path on the host, with no A2A framing: for the card and for negative routes. */
  readonly raw: (
    verb: string,
    path: string,
    options?: Options,
  ) => Promise<Response>;
};

const hosts: Host[] = [];

export async function stopAll(): Promise<void> {
  for (const h of hosts.splice(0)) await h.stop();
}

export async function serve(options: {
  readonly agents: Readonly<Record<string, Agent<never, unknown>>>;
  readonly a2a: A2aOptions;
  readonly store?: Store;
  /** Omitted: the host has no authenticate at all, so every principal route is 401. */
  readonly withAuth?: boolean;
}): Promise<Served> {
  const store = options.store ?? sqlite(":memory:");
  const h = host({
    store,
    agents: options.agents,
    a2a: options.a2a,
    ...(options.withAuth === false ? {} : { authenticate }),
  });
  hosts.push(h);
  await h.ready();
  return served(h, store);
}

/** A host that is already built and ready: for the two-hosts-one-store drill. */
export function served(h: Host, store: Store): Served {
  const raw = (
    verb: string,
    path: string,
    o: Options = {},
  ): Promise<Response> => h.fetch(request(verb, path, o));
  return {
    host: h,
    store,
    raw,
    rpc: (method, params, o = {}) =>
      raw("POST", `/a2a/support`, {
        ...o,
        body: { jsonrpc: "2.0", id: 1, method, params },
      }),
    http: (verb, path, o = {}) => raw(verb, `/a2a/support${path}`, o),
  };
}

function request(verb: string, path: string, o: Options): Request {
  const query =
    o.versionQuery === undefined
      ? ""
      : `${path.includes("?") ? "&" : "?"}${VERSION_HEADER}=${o.versionQuery}`;
  const version = o.version === undefined ? A2A_VERSION : o.version;
  return new Request(`http://host.test${path}${query}`, {
    method: verb,
    headers: {
      ...(o.as === undefined ? {} : { "x-principal": JSON.stringify(o.as) }),
      ...(version === null ? {} : { [VERSION_HEADER]: version }),
      ...(o.body === undefined && o.rawBody === undefined
        ? {}
        : { "content-type": "application/json" }),
      ...o.headers,
    },
    ...(o.rawBody !== undefined
      ? { body: o.rawBody }
      : o.body === undefined
        ? {}
        : { body: JSON.stringify(o.body) }),
  });
}

/** A SendMessage request message with one text part. */
export function message(
  messageId: string,
  text: string,
  extra: Readonly<Record<string, unknown>> = {},
): unknown {
  return {
    message: {
      messageId,
      role: "ROLE_USER",
      parts: [{ text }],
      ...extra,
    },
  };
}

const RpcOk = z.object({ jsonrpc: z.literal("2.0"), result: z.unknown() });
const RpcErr = z.object({
  jsonrpc: z.literal("2.0"),
  error: z.object({ code: z.number(), message: z.string() }),
});
const HttpErr = z.object({ code: z.number(), message: z.string() });

/** A JSON-RPC answer's result. Fails loudly when the answer was an error. */
export async function result(response: Response): Promise<unknown> {
  const body: unknown = await response.json();
  const failed = RpcErr.safeParse(body);
  if (failed.success)
    throw new Error(`expected a result, got ${failed.data.error.message}`);
  return RpcOk.parse(body).result;
}

/** The task a SendMessage answered, in either binding. */
export async function task(response: Response): Promise<Task> {
  const body: unknown = await response.clone().json();
  const rpc = RpcOk.safeParse(body);
  const value = rpc.success ? rpc.data.result : body;
  const wrapped = z.object({ task: z.unknown() }).safeParse(value);
  return z
    .looseObject({ id: z.string(), status: z.looseObject({}) })
    .transform((t) => t as unknown as Task)
    .parse(wrapped.success ? wrapped.data.task : value);
}

/** The A2A error name an answer carries, read from its code, in either binding. */
export async function faultName(response: Response): Promise<string> {
  const body: unknown = await response.json();
  const rpc = RpcErr.safeParse(body);
  const code = rpc.success ? rpc.data.error.code : HttpErr.parse(body).code;
  return errorByCode(code) ?? `unknown code ${code}`;
}

/**
 * Polls GetTask until the task reaches one of `wanted`, and fails the test with the states it did
 * see if it never does. Never skips: a run that does not get there is a failure, not a pass.
 */
export async function reaches(
  on: Served,
  as: Principal,
  taskId: string,
  wanted: readonly string[],
  ms = 10_000,
): Promise<Task> {
  const deadline = Date.now() + ms;
  const seen: string[] = [];
  for (;;) {
    const got = await on.rpc("GetTask", { id: taskId }, { as });
    const found = await task(got);
    if (seen.at(-1) !== found.status.state) seen.push(found.status.state);
    if (wanted.includes(found.status.state)) return found;
    if (Date.now() > deadline)
      throw new Error(
        `task ${taskId} never reached ${wanted.join(" or ")}; it went ${seen.join(" → ")}`,
      );
    await Bun.sleep(20);
  }
}

/** An SSE body's frames as `{id, data}`, read to the end. */
export async function frames(
  response: Response,
): Promise<
  readonly { readonly id: string | undefined; readonly data: unknown }[]
> {
  const text = await response.text();
  return text
    .split("\n\n")
    .filter((block) => block.includes("data: "))
    .map((block) => {
      const id = /^id: (.*)$/m.exec(block)?.[1];
      return {
        id,
        data: JSON.parse(block.slice(block.indexOf("data: ") + 6)),
      };
    });
}
