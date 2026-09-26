import {
  ConfigError,
  type Fetch,
  type ProviderSandbox,
  remoteSandbox,
} from "@threads/core/adapter";
import { dockerDriver } from "./driver";
import { engine } from "./engine";
import { resolveSocket, socketFetch } from "./socket";

// docker(): a sandbox in a local Docker container (spec/api.json docker).
//
// What it declares, and why:
// - Keyless: the Engine API over its unix socket, spoken directly (no SDK, so every request
//   can be fenced). No registry credential is ever sent, so a private image must be pulled.
// - Egress: no network at all by default (egress enforced). `allowInternet: true` puts the
//   container on Docker's bridge, which filters nothing (egress unenforced).
// - Create: the container is named and labelled by its operation key, so a duplicate create
//   is a 409 the ledger reads as its own. A negative lookup can't prove a create in flight
//   won't land, so the lookup is nonfinal.
// - Expiry: none. A container doesn't die on its own, so `expiry.sandboxMs` is null.
// - Termination: confirmed. The supervisor holds one lock per container, and the D-2 probe
//   answers from records it wrote, never from a scan of the guest (records.ts).
// - Snapshots: none yet. Docker's are core's host trees (16C), which is not built here;
//   `docker commit` would miss the /workspace volume, so nothing is declared.

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

export function docker(options: DockerOptions = {}): ProviderSandbox {
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
    dockerDriver(engine(options.fetch ?? socketFetch(at)), limits),
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
