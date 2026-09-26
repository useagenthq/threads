import {
  ConfigError,
  type Fetch,
  type ProviderSandbox,
  remoteSandbox,
} from "@threads/core/adapter";
import { dockerDriver } from "./driver";
import { engine } from "./engine";
import type { Supervisor } from "./pins";
import { resolveSocket, socketFetch } from "./socket";

// What docker() builds, with the supervisor it injects as an argument (index.ts passes the
// binaries this package ships). Everything the adapter declares, and why, is in index.ts.

/** The default image: a verified multi-arch index with bash, git, python3 and tar. */
const DEFAULT_IMAGE =
  "node:22-bookworm@sha256:363e1587494626837fa7f9a23bdb453d13b0ff3c67c705c2805cfc69c2d2fad7";

export type DockerOptions = {
  /** Any linux/amd64 or linux/arm64 image, as a tag or a digest. */
  readonly image?: string;
  /** Puts the container on Docker's default network (egress unenforced). Defaults to false. */
  readonly allowInternet?: boolean;
  /** How many CPUs the container may use (Docker's NanoCpus). Omitted: no limit. */
  readonly cpus?: number;
  /** How much memory, in MiB, with swap held to the same value. Omitted: no limit. */
  readonly memoryMb?: number;
  /** The transport under the fence. Defaults to the Engine API over the discovered socket. */
  readonly fetch?: Fetch;
};

function nanoCpus(cpus: number | undefined): number | undefined {
  if (cpus === undefined) return undefined;
  if (!(cpus > 0))
    throw new ConfigError(
      "invalid_config",
      `docker: cpus must be greater than 0, not ${cpus}`,
    );
  return Math.round(cpus * 1e9);
}

function memoryBytes(memoryMb: number | undefined): number | undefined {
  if (memoryMb === undefined) return undefined;
  if (!Number.isInteger(memoryMb) || memoryMb < 64)
    throw new ConfigError(
      "invalid_config",
      `docker: memoryMb must be an integer of at least 64, not ${memoryMb}`,
    );
  return memoryMb * 1024 * 1024;
}

export function dockerSandbox(
  options: DockerOptions,
  supervisor: Supervisor,
): ProviderSandbox {
  const internet = options.allowInternet ?? false;
  const limits = {
    image: options.image ?? DEFAULT_IMAGE,
    internet,
    nanoCpus: nanoCpus(options.cpus),
    memoryBytes: memoryBytes(options.memoryMb),
  };
  // Resolved once, at the first request that needs it: setup() only proves it can be found.
  let socket: string | undefined;
  const at = () => (socket ??= resolveSocket());
  const sandbox = remoteSandbox(
    dockerDriver(engine(options.fetch ?? socketFetch(at)), limits, supervisor),
    {
      provider: "docker",
      egress: internet ? "unenforced" : "enforced",
      browser: "none",
      desktop: "none",
    },
    { sandboxMs: null, snapshotMs: null },
  );
  return {
    ...sandbox,
    // The socket is looked for in env and on disk; no connection is opened here.
    setup: async () => {
      resolveSocket();
    },
  };
}
