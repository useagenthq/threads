#!/usr/bin/env bash
# Builds the Docker sandbox supervisor for linux-amd64 and linux-arm64 and pins its sha256.
# The binaries are build output (gitignored); docker/supervise/binaries.json is the contract.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
src="$root/docker/supervise"
out="$src/dist"
docker="${DOCKER:-docker}"

rm -rf "$out"
for arch in amd64 arm64; do
  "$docker" build --platform "linux/$arch" --output "type=local,dest=$out/linux-$arch" "$src"
done

hash_of() { shasum -a 256 "$1" 2>/dev/null | cut -d' ' -f1 || sha256sum "$1" | cut -d' ' -f1; }

printf '{\n  "linux-amd64": "%s",\n  "linux-arm64": "%s"\n}\n' \
  "$(hash_of "$out/linux-amd64/supervise")" \
  "$(hash_of "$out/linux-arm64/supervise")" > "$src/binaries.json"

# Both packages ship the binaries; each checks the sha before every injection.
for dest in "$root/typescript/packages/docker/bin" "$root/python/src/threads/adapters/sandboxes/docker/bin"; do
  mkdir -p "$dest"
  cp "$out/linux-amd64/supervise" "$dest/supervise-linux-amd64"
  cp "$out/linux-arm64/supervise" "$dest/supervise-linux-arm64"
done

cat "$src/binaries.json"
