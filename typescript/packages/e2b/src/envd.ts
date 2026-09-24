import type { Sinks, Started } from "@threads/core/adapter";
import { serverStream, unary } from "./connect";
import { follow } from "./process";
import { bounded, type Send } from "./transport";
import { type Code, type Described, E2bError } from "./wire";

// One sandbox's envd, E2B's agent inside it, as Python's `e2b/envd.py` speaks to it: processes
// through the Connect `process.Process` service, files through envd's HTTP `/files` API.
// Processes run as root, each tagged with its process key so a stop can name it. Envd gets
// the sandbox's own access token and never the E2B API key: envd runs inside the sandbox.
// The shapes are `spec/envd/envd.yaml` and `spec/envd/process/process.proto` in e2b-dev/E2B at
// ccaf9fc0ffe6ac39c7ec786af7608ab1de19467b (tag e2b@2.51.0).

const USER = "root";
export const ENVD_PORT = 49983;
/** Domains whose sandboxes are reached through one proxy host (the e2b SDK's list). */
const PROXIED = new Set(["e2b.app", "e2b.dev", "e2b.pro", "e2b-staging.dev"]);
/** Envd's keepalive for a long exec stream, in seconds (the e2b SDK's). */
const KEEPALIVE_S = "50";

/** Where a sandbox's envd answers, as the e2b SDK's `get_sandbox_url` builds it. */
export function envdUrl(id: string, domain: string): string {
  return PROXIED.has(domain)
    ? `https://sandbox.${domain}`
    : `https://${ENVD_PORT}-${id}.${domain}`;
}

export type Process = {
  readonly argv: readonly [string, ...string[]];
  readonly env: Readonly<Record<string, string>>;
  readonly cwd: string;
  readonly tag: string | undefined;
};

export type Envd = {
  /** Starts a process; resolves once envd reports it started. Output reaches `sinks`. */
  readonly start: (process: Process, sinks: Sinks) => Promise<Started>;
  /** SIGKILL to the process started under `tag`. false: none runs. */
  readonly signal: (tag: string) => Promise<boolean>;
  readonly upload: (path: string, data: Uint8Array) => Promise<void>;
  readonly download: (path: string) => Promise<Uint8Array>;
};

/** A file reply's status as a sandbox error code (Python's `_checked`). */
function fileCode(status: number, text: string): Code {
  switch (status) {
    case 404:
      return "not_found";
    case 400:
      return text.toLowerCase().includes("directory")
        ? "is_directory"
        : "invalid_path";
    case 401:
    case 403:
      return "permission_denied";
    case 413:
    case 507:
      return "too_large";
    default:
      return "unavailable";
  }
}

async function checked(res: Response, path: string): Promise<Uint8Array> {
  if (res.status === 200) return new Uint8Array(await res.arrayBuffer());
  const text = (await res.text()).slice(0, 500);
  throw new E2bError(
    fileCode(res.status, text),
    `${path}: envd ${res.status}: ${text}`,
  );
}

/** The envd of `sandbox`, reached at `url`. */
export function envd(send: Send, url: string, sandbox: Described): Envd {
  const headers: Record<string, string> = {
    "E2b-Sandbox-Id": sandbox.sandboxID,
    "E2b-Sandbox-Port": String(ENVD_PORT),
    Authorization: `Basic ${btoa(`${USER}:`)}`,
  };
  if (sandbox.envdAccessToken != null)
    headers["X-Access-Token"] = sandbox.envdAccessToken;
  const files = (path: string) =>
    `${url}/files?${new URLSearchParams({ path, username: USER })}`;

  return {
    start: async ({ argv: [cmd, ...args], env, cwd, tag }, sinks) => {
      // Protobuf JSON leaves empty fields out: the same JSON Python's generated stubs send.
      const config = {
        cmd,
        ...(args.length > 0 ? { args } : {}),
        ...(Object.keys(env).length > 0 ? { envs: env } : {}),
        cwd,
      };
      const events = await serverStream(
        send,
        `${url}/process.Process/Start`,
        { ...headers, "Keepalive-Ping-Interval": KEEPALIVE_S },
        tag === undefined ? { process: config } : { process: config, tag },
      );
      return follow(events, sinks);
    },
    signal: async (tag) => {
      const sent = await unary(
        send,
        `${url}/process.Process/SendSignal`,
        headers,
        {
          process: { tag },
          signal: "SIGNAL_SIGKILL",
        },
      );
      if (sent.ok) return true;
      if (sent.code === "not_found") return false;
      throw new E2bError("unavailable", `envd: ${sent.code}: ${sent.message}`);
    },
    upload: async (path, data) => {
      const form = new FormData();
      form.append("file", new Blob([data.slice()]), path);
      await checked(
        await send(
          files(path),
          bounded({ method: "POST", headers, body: form }),
        ),
        path,
      );
    },
    download: async (path) =>
      checked(
        await send(files(path), bounded({ method: "GET", headers })),
        path,
      ),
  };
}
