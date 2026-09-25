import { sha256Hex } from "../../hash";

// The POSIX scripts a remote sandbox runs through its provider's `sh -c`: one
// implementation of exec, the tree export and import, and the file checks for every provider. Each script's first
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
 * opened and then unlinked, or /dev/null. argv[0] is resolved on the provider's PATH first:
 * `env -i` clears PATH, and execvp would then search only its default path, missing e.g.
 * python3 in /usr/local/bin. The image requirements are in spec/schema/README.md
 * ("Sandbox image").
 */
export function execScript(spec: ExecSpec): string {
  const input = spec.stdin ? stdinPath(spec.processKey) : "/dev/null";
  const env = Object.entries(spec.env).map(([k, v]) => quote(`${k}=${v}`));
  const [cmd = "", ...rest] = spec.command;
  const words = ["exec env -i", ...env, '"$__t_c"', ...rest.map(quote)];
  return [
    ": threads-exec",
    `cd ${quote(spec.cwd)} || exit 126`,
    `exec 0<${quote(input)} || exit 126`,
    ...(spec.stdin ? [`rm -f ${quote(input)}`] : []),
    // A PATH walk for an executable file, as Python's wrapper does: `command -v` would also
    // accept a shell builtin, which `env` then can't run.
    `__t_x=${quote(cmd)}`,
    `case "$__t_x" in */*) __t_c=$__t_x ;; *) __t_c=; IFS=:; for __t_p in $PATH; do if [ -f "$__t_p/$__t_x" ] && [ -x "$__t_p/$__t_x" ]; then __t_c=$__t_p/$__t_x; break; fi; done; unset IFS ;; esac`,
    `[ -n "$__t_c" ] || { echo "threads: command not found: $__t_x" >&2; exit 127; }`,
    words.join(" "),
  ].join("\n");
}

/** Streams /workspace as a tar archive on stdout (SandboxSession.exportTree). */
export const EXPORT_TREE_SCRIPT: string = [
  ": threads-export-tree",
  `exec tar -cf - -C ${WORKSPACE} .`,
].join("\n");

/** Where an import's archive is uploaded; the import script removes it. */
export function treePath(): string {
  return `/tmp/threads-tree-${crypto.randomUUID()}.tar`;
}

/**
 * Extracts the uploaded archive into /workspace, keeping its modes (-p, which the host builder
 * masked to 0o777) but not its owner, then removes it whatever tar answered.
 */
export function importTreeScript(path: string): string {
  const p = quote(path);
  return [
    `: threads-import-tree ${p}`,
    `tar -xpf ${p} -C ${WORKSPACE} --no-same-owner`,
    "__t_s=$?",
    `rm -f ${p}`,
    'exit "$__t_s"',
  ].join("\n");
}

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
