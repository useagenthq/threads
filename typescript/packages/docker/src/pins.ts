import { readFileSync } from "node:fs";
import { sha256Hex } from "@threads/core/adapter";
import { unavailable } from "./wire";

// Where the supervisor comes from, and the sha256 it must hash to before a byte of it is
// injected. The pins are source (docker/supervise/binaries.json is the same contract, and
// docker.test.ts fails when the two drift); the binaries under bin/ are build output of
// scripts/build-supervisor.sh. The directory and the pins are arguments so the check can be
// proven over bytes a test wrote, with no build output in the way.

export type Arch = "amd64" | "arm64";

export const SUPERVISOR_SHA256: Readonly<Record<Arch, string>> = {
  amd64: "be6997cac935a56bae4ccecc0efb858a145d6349e7a6745645e681be0df55491",
  arm64: "0bc5ab34f374decebad166ddd039796744c19c601fee0478a108dd5e4907ea95",
};

/** The supervisor binaries an adapter may inject, each behind its pinned sha256. */
export type Supervisor = {
  /** The binary for `arch`, refused unless it hashes to its pin. */
  readonly binary: (arch: Arch) => Uint8Array;
};

/** The `supervise-linux-<arch>` files in `dir`, each checked against `pins`. */
export function supervisorIn(
  dir: URL,
  pins: Readonly<Record<Arch, string>>,
): Supervisor {
  return {
    binary: (arch) => {
      let bytes: Uint8Array;
      try {
        bytes = new Uint8Array(
          readFileSync(new URL(`supervise-linux-${arch}`, dir)),
        );
      } catch {
        throw unavailable(
          `@threads/docker ships no supervisor for linux-${arch}: run scripts/build-supervisor.sh`,
        );
      }
      if (sha256Hex(bytes) !== pins[arch])
        throw unavailable(
          "the injected supervisor does not match its pinned sha256",
        );
      return bytes;
    },
  };
}

/** What `docker()` injects: the binaries this package ships, under the pins above. */
export const SHIPPED: Supervisor = supervisorIn(
  new URL("../bin/", import.meta.url),
  SUPERVISOR_SHA256,
);
