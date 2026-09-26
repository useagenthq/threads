import type { ProviderSandbox } from "@threads/core/adapter";
import { SHIPPED } from "./pins";
import { type DockerOptions, dockerSandbox } from "./sandbox";

// docker(): a sandbox in a local Docker container (spec/api.json docker).
//
// What it declares, and why:
// - Keyless: the Engine API over its unix socket, spoken directly (no SDK, so every request
//   can be fenced). No registry credentials are ever sent, so a private image must be pulled.
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

export type { DockerOptions };

export function docker(options: DockerOptions = {}): ProviderSandbox {
  return dockerSandbox(options, SHIPPED);
}
