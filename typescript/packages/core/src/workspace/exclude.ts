// The workspace deny-list (spec/schema/README.md, Workspace inputs, rules 1 and 3), matched per
// path segment: exact key names only, so id_utils/ and id_token.ts are kept.

const EXACT: ReadonlySet<string> = new Set([
  "id_rsa",
  "id_dsa",
  "id_ecdsa",
  "id_ed25519",
  "id_ecdsa_sk",
  "id_ed25519_sk",
  ".npmrc",
  ".pypirc",
  ".netrc",
  ".aws",
  ".ssh",
]);

/** Segment `i` of a path's `parts` is deny-listed (rule 3). */
export function denied(parts: readonly string[], i: number): boolean {
  const name = parts[i] ?? "";
  return (
    EXACT.has(name) ||
    name.startsWith(".env") ||
    name.endsWith(".pem") ||
    name.endsWith(".key") ||
    (name === "config.json" && parts[i - 1] === ".docker")
  );
}

/** Rules 1 and 3 on the segments of `path` from `start`: a `.git` or deny-listed segment. */
export function excluded(path: string, start = 0): boolean {
  const parts = path.split("/");
  return parts.some((p, i) => i >= start && (p === ".git" || denied(parts, i)));
}
