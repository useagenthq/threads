import { z } from "zod";
import { sha256Hex } from "../../hash";
import { type ManifestEntry, ManifestEntry as ManifestSchema } from "../script";

// The POSIX scripts a remote sandbox runs through its provider's `sh -c`: one
// implementation of exec, the manifest and file checks for every provider. Each script's first
// line is a `:` no-op naming what it is, so it reads clearly in a process list. Values are
// single-quoted, so no host value is ever interpreted by the shell. Nothing here proves
// termination or quiescence: an in-guest scan is forgeable, so those come only from provider
// control-plane primitives (driver.ts).

export const WORKSPACE = "/workspace";

/** Exit codes of the file checks. */
export const FILE_EXIT = {
  not_found: 20,
  is_directory: 21,
  permission_denied: 22,
} as const;

/** One shell word: the value single-quoted, so the shell never expands it. */
export function quote(value: string): string {
  return `'${value.replaceAll("'", `'\\''`)}'`;
}

/** An absolute path with `.` and `..` resolved, relative to /workspace; undefined above root. */
export function sandboxPath(path: string): string | undefined {
  if (path.includes("\0")) return undefined;
  const out: string[] = [];
  const full = path.startsWith("/") ? path : `${WORKSPACE}/${path}`;
  for (const part of full.split("/")) {
    if (part === "" || part === ".") continue;
    if (part !== "..") out.push(part);
    else if (out.pop() === undefined) return undefined;
  }
  return `/${out.join("/")}`;
}

/** Where an exec's stdin is staged: named by its process key, removed once opened. */
export function stdinPath(processKey: string): string {
  return `/tmp/threads-stdin-${sha256Hex(processKey).slice(0, 32)}`;
}

export const INIT_SCRIPT: string = `: threads-init\nmkdir -p ${WORKSPACE}`;

export type ExecSpec = {
  readonly command: readonly string[];
  readonly cwd: string;
  readonly env: Readonly<Record<string, string>>;
  readonly processKey: string;
  readonly stdin: boolean;
};

/**
 * The command with exactly `env` (nothing inherited), in `cwd`. Stdin is the staged file,
 * opened and then unlinked, or /dev/null.
 */
export function execScript(spec: ExecSpec): string {
  const input = spec.stdin ? stdinPath(spec.processKey) : "/dev/null";
  const env = Object.entries(spec.env).map(([k, v]) => quote(`${k}=${v}`));
  const words = ["exec env -i", ...env, ...spec.command.map(quote)];
  return [
    ": threads-exec",
    `cd ${quote(spec.cwd)} || exit 126`,
    `exec 0<${quote(input)} || exit 126`,
    ...(spec.stdin ? [`rm -f ${quote(input)}`] : []),
    words.join(" "),
  ].join("\n");
}

/** Prints the /workspace file tree as NUL-terminated `mode\tsize\tsha256\tpath` records. */
export const MANIFEST_SCRIPT: string = [
  ": threads-manifest",
  `cd ${WORKSPACE} || exit 1`,
  `find . -type f -exec sh -c 'for f do printf "%s\\t%s\\t%s\\t%s\\0" "$(stat -c %a "$f")" "$(stat -c %s "$f")" "$(sha256sum < "$f" | cut -c1-64)" "\${f#./}"; done' sh {} +`,
].join("\n");

/** Checks a path before a write: a directory or an unwritable file is a typed failure. */
export function checkWriteScript(path: string): string {
  const p = quote(path);
  return [
    `: threads-check-write ${p}`,
    `[ -d ${p} ] && exit ${FILE_EXIT.is_directory}`,
    `mkdir -p "$(dirname ${p})" 2>/dev/null || exit ${FILE_EXIT.permission_denied}`,
    `[ ! -e ${p} ] || [ -w ${p} ] || exit ${FILE_EXIT.permission_denied}`,
    "exit 0",
  ].join("\n");
}

/** Checks a path before a read. */
export function checkReadScript(path: string): string {
  const p = quote(path);
  return [
    `: threads-check-read ${p}`,
    `[ -d ${p} ] && exit ${FILE_EXIT.is_directory}`,
    `[ -e ${p} ] || exit ${FILE_EXIT.not_found}`,
    `[ -r ${p} ] || exit ${FILE_EXIT.permission_denied}`,
    "exit 0",
  ].join("\n");
}

const utf8 = new TextDecoder();
const Octal = z.string().regex(/^[0-7]{1,6}$/);
const Decimal = z.string().regex(/^[0-9]{1,15}$/);

/** Parses the manifest script's output (a sandbox response: a trust boundary), sorted by path. */
export function parseManifest(
  output: Uint8Array,
): readonly ManifestEntry[] | undefined {
  const entries: ManifestEntry[] = [];
  for (const record of utf8.decode(output).split("\0")) {
    if (record === "") continue;
    const [mode, size, sha256, ...path] = record.split("\t");
    const parsed = ManifestSchema.safeParse({
      mode: Number.parseInt(Octal.safeParse(mode).data ?? "x", 8),
      size: Number(Decimal.safeParse(size).data ?? "x"),
      sha256,
      path: path.join("\t"),
    });
    if (!parsed.success) return undefined;
    entries.push(parsed.data);
  }
  return entries.toSorted((a, b) =>
    a.path < b.path ? -1 : a.path > b.path ? 1 : 0,
  );
}
