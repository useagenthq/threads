import { JsonValue } from "@threads/core/adapter";
import { z } from "zod";
import { bounded, type Send } from "./transport";
import { Described, E2bError, excerpt, parsed } from "./wire";

// E2B's control plane (its REST API at api.<domain>), as Python's `e2b/control.py` speaks it:
// statuses an operation expects are values, any other status is an E2bError. The endpoints
// and bodies are `spec/openapi.yml` in e2b-dev/E2B at ccaf9fc0ffe6ac39c7ec786af7608ab1de19467b
// (tag e2b@2.51.0).

/** The sandbox metadata entry that names the operation that created it. */
export const OPERATION_KEY = "threads_operation_key";
/** The listing's page size: E2B's maximum. */
const PAGE = 100;
/** Pages one lookup follows before it gives up: an error, never a false absence. */
const MAX_PAGES = 100;

const Listed = z.array(
  z.object({
    sandboxID: z.string().min(1),
    metadata: z.record(z.string(), JsonValue).nullish(),
  }),
);
const Snapshot = z.object({ snapshotID: z.string().min(1) });

export type CreateOptions = {
  readonly timeoutS: number;
  readonly internet: boolean;
};

export type Control = {
  /** A sandbox from `template` (a template or snapshot id). null: E2B has no such template. */
  readonly create: (
    template: string,
    key: string,
    options: CreateOptions,
  ) => Promise<Described | null>;
  /** The live or paused sandboxes created under `key`, from every page of the listing. */
  readonly find: (key: string) => Promise<readonly string[]>;
  /** null: no such sandbox. */
  readonly describe: (id: string) => Promise<Described | null>;
  /** false: it was already gone. */
  readonly kill: (id: string) => Promise<boolean>;
  /** Snapshots the sandbox under `name`; its snapshot id. */
  readonly snapshot: (id: string, name: string) => Promise<string>;
  /** Deletes a snapshot (E2B keeps one as a template). false: it was already gone. */
  readonly deleteTemplate: (id: string) => Promise<boolean>;
};

async function unexpected(res: Response): Promise<E2bError> {
  return new E2bError(
    "unavailable",
    `E2B API ${res.status}: ${await excerpt(res)}`,
  );
}

/** The listing's query, in the order and encoding Python's SDK sends it. */
function listing(key: string, token: string | undefined): string {
  const metadata = new URLSearchParams({
    [OPERATION_KEY]: encodeURIComponent(key),
  });
  const query = new URLSearchParams({
    metadata: metadata.toString(),
    state: "running,paused",
  });
  if (token !== undefined) query.set("nextToken", token);
  query.set("limit", String(PAGE));
  return query.toString();
}

/** The control plane at `api`, authenticated by the key `apiKey` reads per call. */
export function control(
  send: Send,
  api: string,
  apiKey: () => string,
): Control {
  const call = (method: string, path: string, body?: unknown) =>
    send(
      `${api}${path}`,
      bounded({
        method,
        headers: {
          "X-API-KEY": apiKey(),
          ...(body === undefined ? {} : { "content-type": "application/json" }),
        },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      }),
    );
  const sandbox = async (res: Response, ok: number) => {
    if (res.status === 404) return null;
    if (res.status !== ok) throw await unexpected(res);
    return parsed(Described, await res.text(), "sandbox");
  };
  const gone = async (res: Response, ok: number) => {
    if (res.status === 404) return false;
    if (res.status !== ok) throw await unexpected(res);
    return true;
  };

  const page = async (key: string, token: string | undefined) => {
    const res = await call("GET", `/v2/sandboxes?${listing(key, token)}`);
    if (res.status !== 200) throw await unexpected(res);
    const listed = parsed(Listed, await res.text(), "sandbox list");
    const ids = listed
      .filter((s) => s.metadata?.[OPERATION_KEY] === key)
      .map((s) => s.sandboxID);
    return { ids, next: res.headers.get("x-next-token") || undefined };
  };

  return {
    create: async (template, key, options) =>
      sandbox(
        await call("POST", "/v2/sandboxes", {
          templateID: template,
          timeout: options.timeoutS,
          autoPause: false,
          autoPauseMemory: true,
          allow_internet_access: options.internet,
          metadata: { [OPERATION_KEY]: key },
          envVars: {},
        }),
        201,
      ),
    find: async (key) => {
      const found: string[] = [];
      let token: string | undefined;
      for (let n = 0; n < MAX_PAGES; n++) {
        const { ids, next } = await page(key, token);
        found.push(...ids);
        if (next === undefined) return found;
        token = next;
      }
      throw new E2bError(
        "unavailable",
        `E2B's sandbox listing didn't end within ${MAX_PAGES} pages`,
      );
    },
    describe: async (id) =>
      sandbox(await call("GET", `/sandboxes/${encodeURIComponent(id)}`), 200),
    kill: async (id) =>
      gone(await call("DELETE", `/sandboxes/${encodeURIComponent(id)}`), 204),
    snapshot: async (id, name) => {
      const path = `/sandboxes/${encodeURIComponent(id)}/snapshots`;
      const res = await call("POST", path, { name });
      if (res.status !== 201) throw await unexpected(res);
      return parsed(Snapshot, await res.text(), "snapshot").snapshotID;
    },
    deleteTemplate: async (id) =>
      gone(await call("DELETE", `/templates/${encodeURIComponent(id)}`), 204),
  };
}
