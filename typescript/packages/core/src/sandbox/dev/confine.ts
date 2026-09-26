import { spawnSync } from "node:child_process";
import { err, ok, type Result } from "../../result";

// The OS confinement devSandbox() runs every command inside (lane 16 D3). It is required: a
// platform without one is refused at setup, never run unconfined. Nothing here proves a
// process ended; termination stays unconfirmed.
//
// Linux is bubblewrap: private pid, ipc, uts and cgroup namespaces, no network unless the
// caller asked for it, a private /proc, /tmp and /run, read-only system paths, the sandbox
// directory as /workspace and no home, an exact environment, capabilities dropped, and the
// wrapper dying with its parent.
//
// macOS is sandbox-exec with a deny-by-default profile: the same reachable paths, network
// denied, other processes' info denied, and a fixed list of Mach lookups. macOS has no mount
// namespace, so the directory keeps its host path there and `guest()` rewrites /workspace to
// it (Linux returns the path unchanged).

export type ConfinedSpec = {
  /** The sandbox's host directory, which becomes its /workspace. */
  readonly dir: string;
  readonly command: readonly string[];
  /** The working directory as the command sees it (a `guest()` path). */
  readonly cwd: string;
  /** Exactly this environment; nothing of the host's is inherited. */
  readonly env: Readonly<Record<string, string>>;
};

export type Confinement = {
  /** The program that confines, named in a capability_missing message. */
  readonly tool: string;
  /** The confined argv to spawn. */
  readonly wrap: (spec: ConfinedSpec) => readonly string[];
  /**
   * The environment the wrapper process itself is spawned with. bwrap carries the call's env
   * through --setenv after --clearenv; sandbox-exec has no such flag, so the call's env is the
   * spawn's. Either way the command sees exactly the call's names and nothing of the host's.
   */
  readonly spawnEnv: (
    env: Readonly<Record<string, string>>,
  ) => Record<string, string>;
  /** The path a confined command sees for the sandbox path `path`. */
  readonly guest: (dir: string, path: string) => string;
};

/** The confinement program each platform uses when the caller names none. */
export const DEFAULT_TOOL: Readonly<Record<string, string>> = {
  linux: "bwrap",
  darwin: "sandbox-exec",
};

export const WORKSPACE = "/workspace";

/** Read-only system paths; `-try` skips one the host doesn't have. */
const LINUX_SYSTEM = ["/usr", "/bin", "/sbin", "/lib", "/lib64", "/lib32"];

/** A minimal /etc: name resolution, certificates and the time zone, never the host's config. */
const LINUX_ETC = [
  "/etc/alternatives",
  "/etc/ca-certificates",
  "/etc/group",
  "/etc/localtime",
  "/etc/nsswitch.conf",
  "/etc/passwd",
  "/etc/resolv.conf",
  "/etc/ssl",
];

function bwrapArgs(spec: ConfinedSpec, allowInternet: boolean): string[] {
  return [
    "--unshare-pid",
    "--unshare-ipc",
    "--unshare-uts",
    "--unshare-cgroup-try",
    ...(allowInternet ? [] : ["--unshare-net"]),
    "--proc",
    "/proc",
    // /dev is not in the sub-lane's list, but without /dev/null nothing runs.
    "--dev",
    "/dev",
    "--tmpfs",
    "/tmp",
    "--tmpfs",
    "/run",
    ...[...LINUX_SYSTEM, ...LINUX_ETC].flatMap((at) => [
      "--ro-bind-try",
      at,
      at,
    ]),
    "--bind",
    spec.dir,
    WORKSPACE,
    "--chdir",
    spec.cwd,
    "--clearenv",
    ...Object.entries(spec.env).flatMap(([name, value]) => [
      "--setenv",
      name,
      value,
    ]),
    "--die-with-parent",
    "--new-session",
    "--cap-drop",
    "ALL",
    "--",
    ...spec.command,
  ];
}

/** A Seatbelt string literal: only `"` and `\` are special. */
function seatbelt(value: string): string {
  return `"${value.replaceAll("\\", "\\\\").replaceAll('"', '\\"')}"`;
}

/**
 * Read-only system paths. The root directory itself is read: dyld resolves through it, and
 * without it every command aborts before main.
 */
const MACOS_READ = [
  "/usr",
  "/bin",
  "/sbin",
  "/System",
  "/Library",
  "/dev",
  "/private/etc",
  "/private/var/db",
  "/private/var/select",
  "/opt/homebrew",
  "/opt/local",
];

/** The Mach services a command needs to start and to look a user up; nothing else is reachable. */
const MACOS_MACH = [
  "com.apple.system.opendirectoryd.libinfo",
  "com.apple.system.opendirectoryd.membership",
  "com.apple.system.notification_center",
  "com.apple.system.logger",
];

/** The character devices a command writes to; the rest of /dev stays read-only. */
const MACOS_WRITE_DEV = [
  "/dev/null",
  "/dev/zero",
  "/dev/tty",
  "/dev/dtracehelper",
];

function profile(dir: string, allowInternet: boolean): string {
  return [
    "(version 1)",
    "(deny default)",
    "(allow process-fork)",
    "(allow process-exec*)",
    "(allow sysctl-read)",
    "(allow ipc-posix-shm*)",
    "(allow signal (target self))",
    "(allow process-info-pidinfo (target self))",
    // stat() of a path's ancestors, which path resolution needs. It reads no content and
    // lists no directory: a denied file's bytes stay denied.
    "(allow file-read-metadata)",
    `(allow file-read* (literal "/") ${MACOS_READ.map((at) => `(subpath ${seatbelt(at)})`).join(" ")})`,
    `(allow file-read* file-write* (subpath ${seatbelt(dir)}))`,
    `(allow file-write-data ${MACOS_WRITE_DEV.map((at) => `(literal ${seatbelt(at)})`).join(" ")})`,
    `(allow mach-lookup ${MACOS_MACH.map((n) => `(global-name ${seatbelt(n)})`).join(" ")})`,
    allowInternet ? "(allow network*)" : "(deny network*)",
  ].join("\n");
}

function linux(allowInternet: boolean, tool: string): Confinement {
  return {
    tool,
    wrap: (spec) => [tool, ...bwrapArgs(spec, allowInternet)],
    spawnEnv: () => ({}),
    guest: (_dir, path) => path,
  };
}

/** /workspace as a whole path component, which macOS rewrites to the host directory. */
const GUEST_ROOT = /\/workspace(?=$|[/\s:'"])/g;

function macos(allowInternet: boolean, tool: string): Confinement {
  // No mount namespace: the directory keeps its host path there, so every /workspace a
  // command is given names it instead. A command's own output still shows the host path.
  const guest = (dir: string, path: string): string =>
    path.replaceAll(GUEST_ROOT, dir);
  return {
    tool,
    wrap: (spec) => [
      tool,
      "-p",
      profile(spec.dir, allowInternet),
      "--",
      ...spec.command.map((word) => guest(spec.dir, word)),
    ],
    spawnEnv: (env) => ({ ...env }),
    guest,
  };
}

/** The platform's confinement, or what is missing (setup answers capability_missing). */
export function confinement(
  allowInternet: boolean,
  tool: string | undefined,
): Result<Confinement, string> {
  const platform: string = process.platform;
  const named = tool ?? DEFAULT_TOOL[platform];
  if (named === undefined || named === "")
    return err(
      `devSandbox() confines with bubblewrap on Linux and sandbox-exec on macOS; ${platform} has neither`,
    );
  if (platform === "linux") return ok(linux(allowInternet, named));
  if (platform === "darwin") return ok(macos(allowInternet, named));
  return err(
    `devSandbox() confines with bubblewrap on Linux and sandbox-exec on macOS; ${platform} has neither`,
  );
}

/**
 * Runs the confinement over `true` in `dir`: what setup proves, so a missing bwrap, a kernel
 * with user namespaces disabled, or a refused profile is named before any agent runs.
 */
export function probe(
  confined: Confinement,
  dir: string,
): string | undefined {
  const argv = confined.wrap({
    dir,
    command: ["/usr/bin/true"],
    cwd: confined.guest(dir, WORKSPACE),
    env: {},
  });
  const [file = "", ...args] = argv;
  const ran = spawnSync(file, args, { cwd: dir, env: {}, encoding: "utf8" });
  if (ran.error !== undefined)
    return `${confined.tool} can't run (${ran.error.message})`;
  if (ran.status === 0) return undefined;
  const why = `${ran.stderr}`.trim().split("\n").at(-1) ?? "";
  return `${confined.tool} refused to confine a command (${why || `exit ${ran.status}`})`;
}
