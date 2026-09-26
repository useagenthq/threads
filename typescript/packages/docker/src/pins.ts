import { readFileSync } from "node:fs";
import { sha256Hex } from "@threads/core/adapter";
import { unavailable } from "./wire";

// The supervisor binaries this package ships, and the sha256 each is checked against before
// it is injected. The pins are source (docker/supervise/binaries.json is the same contract,
// and docker.test.ts fails when the two drift); the binaries under bin/ are build output of
// scripts/build-supervisor.sh.

export type Arch = "amd64" | "arm64";

export const SUPERVISOR_SHA256: Readonly<Record<Arch, string>> = {
  amd64: "be6997cac935a56bae4ccecc0efb858a145d6349e7a6745645e681be0df55491",
  arm64: "0bc5ab34f374decebad166ddd039796744c19c601fee0478a108dd5e4907ea95",
};

/** The binary for `arch`, refused unless it hashes to its pin. */
export function supervisorBinary(arch: Arch): Uint8Array {
  const at = new URL(`../bin/supervise-linux-${arch}`, import.meta.url);
  let bytes: Uint8Array;
  try {
    bytes = new Uint8Array(readFileSync(at));
  } catch {
    throw unavailable(
      `@threads/docker ships no supervisor for linux-${arch}: run scripts/build-supervisor.sh`,
    );
  }
  if (sha256Hex(bytes) !== SUPERVISOR_SHA256[arch])
    throw unavailable(
      "the injected supervisor does not match its pinned sha256",
    );
  return bytes;
}
