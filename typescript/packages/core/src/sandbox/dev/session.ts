import { type ChildProcess, spawn } from "node:child_process";
import { rmSync, statSync } from "node:fs";
import { SandboxId } from "../../log";
import { err, ok, type Result } from "../../result";
import { admitExec } from "../admit";
import type {
  ExecOutput,
  Failure,
  FileFailure,
  ReleaseFailure,
  SandboxContext,
  SandboxSession,
  SnapshotData,
  Stale,
  Trees,
} from "../protocol";
import { byteStream } from "../remote/bytes";
import { fenceHere, guarded, messageOf } from "../remote/fence";
import { sandboxPath, WORKSPACE } from "../remote/scripts";
import type { Confinement } from "./confine";
import { exportDir, importDir } from "./trees";
import { hostPath, readIn, workspaceParts, writeIn } from "./walk";

// One dev sandbox as a SandboxSession: commands run inside the platform's confinement
// (confine.ts), and everything else is a host file operation on the sandbox's directory that
// never follows a symlink (walk.ts). The fence is checked immediately before the spawn and
// before each file operation's first syscall, so a stale writer spawns nothing and writes
// nothing.

const unavailable = (error: unknown) =>
  ({ code: "unavailable", message: messageOf(error) }) as const;

/**
 * Where a bare command name is looked up. The confinement's environment is exactly the call's,
 * which has no PATH, so argv[0] is resolved here first, as the remote kit's script does inside
 * its sandbox. Linux binds these paths read-only, so the answer is the same inside.
 */
const SYSTEM_PATH: readonly string[] =
  process.platform === "darwin"
    ? [
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
        "/usr/sbin",
        "/sbin",
      ]
    : ["/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"];

function resolveProgram(name: string): string | undefined {
  if (name.includes("/")) return name;
  for (const at of SYSTEM_PATH) {
    const full = `${at}/${name}`;
    try {
      if (statSync(full).isFile()) return full;
    } catch {
      // Not here; try the next directory.
    }
  }
  return undefined;
}

/** What a sandbox answers for a command it has no executable for, as a shell does. */
function notFound(name: string): ExecOutput {
  const out = byteStream();
  const errors = byteStream();
  out.end();
  errors.push(
    new TextEncoder().encode(`threads: command not found: ${name}\n`),
  );
  errors.end();
  return {
    exit_code: Promise.resolve(127),
    stdout: out.chunks,
    stderr: errors.chunks,
  };
}

/** The exec's output, with the exit code resolving once both streams have ended. */
function streamed(child: ChildProcess): ExecOutput {
  const out = byteStream();
  const errors = byteStream();
  const { promise, resolve, reject } = Promise.withResolvers<number>();
  child.stdout?.on("data", (chunk: Buffer) => out.push(chunk));
  child.stderr?.on("data", (chunk: Buffer) => errors.push(chunk));
  child.on("error", (failure) => {
    out.end();
    errors.end();
    reject(failure);
  });
  child.on("close", (code, signal) => {
    out.end();
    errors.end();
    // A killed process has no code; the sandbox layer reads 128+n as a shell does.
    resolve(code ?? (signal === null ? 1 : 137));
  });
  promise.catch(() => undefined);
  return { exit_code: promise, stdout: out.chunks, stderr: errors.chunks };
}

export function devSession(
  dir: string,
  ident: string,
  confined: Confinement,
): SandboxSession & Trees {
  const id = SandboxId.parse(ident);
  const running = new Map<string, ChildProcess>();

  /** One host file operation: the path checked, then the fence, then the syscall. */
  const fileOp = <T>(
    context: SandboxContext,
    path: string,
    body: (parts: readonly string[]) => Result<T, FileFailure>,
  ): Promise<Result<T, FileFailure | Stale>> =>
    guarded<T, FileFailure>(
      context,
      async () => {
        const parts = workspaceParts(path);
        if (!parts.ok) return parts;
        await fenceHere();
        return body(parts.value);
      },
      unavailable,
    );

  const exec: SandboxSession["exec"] = async (given, context, givenOptions) => {
    // Before `guarded`, which would turn a caller bug into a typed `unavailable`.
    const { command, options } = admitExec(given, givenOptions);
    return guarded<
      ExecOutput,
      Failure<"timeout" | "invalid_path" | "unavailable">
    >(
      context,
      async () => {
        const cwd = sandboxPath(options.cwd ?? WORKSPACE);
        if (cwd === undefined)
          return err({ code: "invalid_path", message: `cwd ${options.cwd}` });
        const parts = workspaceParts(cwd);
        if (!parts.ok)
          return err({ code: "invalid_path", message: parts.error.message });
        const at = hostPath(dir, parts.value, false);
        if (!at.ok)
          return err({ code: "invalid_path", message: at.error.message });
        const [name = "", ...rest] = command;
        const program = resolveProgram(name);
        if (program === undefined) return ok(notFound(name));
        const argv = confined.wrap({
          dir,
          command: [program, ...rest],
          cwd: confined.guest(dir, cwd),
          env: options.env,
        });
        const [file = "", ...args] = argv;
        await fenceHere();
        const child = spawn(file, args, {
          cwd: at.value,
          env: confined.spawnEnv(options.env),
          detached: true,
          stdio: ["pipe", "pipe", "pipe"],
        });
        running.set(options.processKey, child);
        child.on("close", () => running.delete(options.processKey));
        child.stdin?.end(options.stdin ?? new Uint8Array());
        return ok(streamed(child));
      },
      unavailable,
    );
  };

  /**
   * Kills the process group the key started. A group leader can still leave a descendant
   * behind (a double fork on macOS, where there is no pid namespace), so this proves nothing
   * and the answer stays unknown, as SandboxInfo declares.
   */
  const terminate: SandboxSession["terminate"] = (processKey, context) =>
    guarded<
      "terminated" | "already_exited" | "unknown",
      Failure<"unavailable">
    >(
      context,
      async () => {
        const child = running.get(processKey);
        await fenceHere();
        if (child?.pid !== undefined) {
          try {
            process.kill(-child.pid, "SIGKILL");
          } catch {
            // Already gone, or in another session: the answer is unknown either way.
          }
        }
        return ok("unknown");
      },
      unavailable,
    );

  const exportTree: Trees["exportTree"] = (context) =>
    guarded<ExecOutput, Failure<"unavailable">>(
      context,
      async () => {
        await fenceHere();
        return exportDir(dir);
      },
      unavailable,
    );

  const importTree: Trees["importTree"] = (tar, context) =>
    guarded<void, Failure<"unavailable">>(
      context,
      async () => {
        await fenceHere();
        return importDir(dir, tar);
      },
      unavailable,
    );

  return {
    id,
    exec,
    terminate,
    upload: (path, data, context) =>
      fileOp<void>(context, path, (parts) => writeIn(dir, parts, data, 0o644)),
    download: (path, context) =>
      fileOp<Uint8Array>(context, path, (parts) => readIn(dir, parts)),
    snapshot: (_operationKey, context) =>
      guarded<
        SnapshotData,
        Failure<"not_quiescent" | "unavailable" | "timeout">
      >(
        context,
        async () =>
          err({
            code: "unavailable",
            message: "the dev sandbox has no snapshots",
          }),
        unavailable,
      ),
    close: (context) =>
      guarded<void, ReleaseFailure>(
        context,
        async () => {
          await fenceHere();
          for (const child of running.values())
            if (child.pid !== undefined) {
              try {
                process.kill(-child.pid, "SIGKILL");
              } catch {
                // Nothing left to kill.
              }
            }
          running.clear();
          rmSync(dir, { recursive: true, force: true });
          return ok(undefined);
        },
        (error) => ({ code: "release_failed", message: messageOf(error) }),
      ),
    exportTree,
    importTree,
  };
}
