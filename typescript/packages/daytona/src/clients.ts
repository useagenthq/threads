import {
  Configuration as ApiConfiguration,
  SandboxApi,
  SnapshotsApi,
} from "@daytona/api-client";
import {
  FileSystemApi,
  ProcessApi,
  Configuration as ToolboxConfiguration,
} from "@daytona/toolbox-api-client";
import { type Fetch, sandboxFetch } from "@threads/core/adapter";

// Daytona's official generated API clients (the transport layer of its SDK), each request sent
// by axios's fetch adapter through threads' fenced fetch. The high-level `Daytona` class builds
// a private axios instance with no transport hook, so it isn't used.

export type Clients = {
  readonly sandboxes: SandboxApi;
  readonly snapshots: SnapshotsApi;
  /** The toolbox of one sandbox, at its toolbox proxy URL. */
  readonly toolbox: (base: string) => {
    readonly process: ProcessApi;
    readonly files: FileSystemApi;
  };
  /** The headers every request carries: the API key, host-side only. */
  readonly headers: Readonly<Record<string, string>>;
};

export function clients(apiUrl: string, apiKey: string, inner: Fetch): Clients {
  const headers = { Authorization: `Bearer ${apiKey}` };
  // One fenced fetch for the adapter's life: axios caches its fetch adapter per function.
  const baseOptions = {
    headers,
    adapter: "fetch",
    env: { fetch: sandboxFetch(inner) },
  };
  const api = new ApiConfiguration({ basePath: apiUrl, baseOptions });
  return {
    sandboxes: new SandboxApi(api),
    snapshots: new SnapshotsApi(api),
    toolbox: (base) => {
      const config = new ToolboxConfiguration({ basePath: base, baseOptions });
      return {
        process: new ProcessApi(config),
        files: new FileSystemApi(config),
      };
    },
    headers,
  };
}

/** The HTTP status behind an axios error, if the provider answered. */
export function statusOf(error: unknown): number | undefined {
  if (typeof error !== "object" || error === null || !("response" in error))
    return undefined;
  const response = error.response;
  return typeof response === "object" &&
    response !== null &&
    "status" in response &&
    typeof response.status === "number"
    ? response.status
    : undefined;
}
