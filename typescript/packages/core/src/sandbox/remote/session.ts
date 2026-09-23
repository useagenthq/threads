import { SandboxId, SnapshotId } from "../../log";
import { err, ok } from "../../result";
import { manifestHash } from "../fake";
import type {
  ExecOutput,
  Failure,
  FileFailure,
  ReleaseFailure,
  SandboxSession,
  SnapshotData,
} from "../protocol";
import { byteStream, joined } from "./bytes";
import type { SandboxDriver } from "./driver";
import { guarded, messageOf } from "./fence";
import {
  checkReadScript,
  checkWriteScript,
  execScript,
  FILE_EXIT,
  MANIFEST_SCRIPT,
  parseManifest,
  sandboxPath,
  stdinPath,
  WORKSPACE,
} from "./scripts";

// One remote sandbox as a SandboxSession (spec/api.json): every operation fences through the
// driver's transport, and everything above the raw provider calls is the shared POSIX kit.

/** A kit script's exit code and stdout, collected: its outputs are small. */
export async function runScript(
  driver: SandboxDriver,
  id: string,
  script: string,
): Promise<{ readonly exit: number; readonly stdout: Uint8Array }> {
  const out = byteStream();
  const started = await driver.run(id, script, {
    stdout: out.push,
    stderr: () => undefined,
  });
  const stdout = joined(out.chunks);
  const exit = await started.exit.finally(out.end);
  return { exit, stdout: await stdout };
}

/** The canonical manifest hash of the sandbox's /workspace, or undefined when unreadable. */
export async function treeHash(
  driver: SandboxDriver,
  id: string,
): Promise<string | undefined> {
  const run = await runScript(driver, id, MANIFEST_SCRIPT);
  const manifest = run.exit === 0 ? parseManifest(run.stdout) : undefined;
  return manifest === undefined ? undefined : manifestHash(manifest);
}

const unavailable = (error: unknown) =>
  ({ code: "unavailable", message: messageOf(error) }) as const;

const FILE_CODES = new Map<number, FileFailure["code"]>([
  [FILE_EXIT.not_found, "not_found"],
  [FILE_EXIT.is_directory, "is_directory"],
  [FILE_EXIT.permission_denied, "permission_denied"],
]);

/** Runs a file check; a typed failure, or undefined when the path may be used. */
async function checked(
  driver: SandboxDriver,
  id: string,
  script: string,
  path: string,
): Promise<FileFailure | undefined> {
  const { exit } = await runScript(driver, id, script);
  if (exit === 0) return undefined;
  return {
    code: FILE_CODES.get(exit) ?? "unavailable",
    message: `${path}: exit ${exit}`,
  };
}

export function remoteSession(
  driver: SandboxDriver,
  provider: string,
  id: string,
): SandboxSession {
  const sandboxId = SandboxId.parse(id);
  // Kills best effort, and never claims it worked: a descendant can drop out of the process
  // the provider tracks, so only an operator can settle it (termination: unconfirmed).
  const terminate: SandboxSession["terminate"] = (processKey, context) =>
    guarded<
      "terminated" | "already_exited" | "unknown",
      Failure<"unavailable">
    >(
      context,
      async () => {
        await driver.stopProcess(id, processKey);
        return ok("unknown");
      },
      unavailable,
    );

  const exec: SandboxSession["exec"] = (command, context, options) =>
    guarded<ExecOutput, Failure<"timeout" | "invalid_path" | "unavailable">>(
      context,
      async () => {
        const cwd = sandboxPath(options.cwd ?? WORKSPACE);
        if (cwd === undefined)
          return err({ code: "invalid_path", message: `cwd ${options.cwd}` });
        if (options.stdin !== undefined)
          await driver.write(id, stdinPath(options.processKey), options.stdin);
        const stdout = byteStream();
        const stderr = byteStream();
        const script = execScript({
          command,
          cwd,
          env: options.env ?? {},
          processKey: options.processKey,
          stdin: options.stdin !== undefined,
        });
        const started = await driver.run(
          id,
          script,
          { stdout: stdout.push, stderr: stderr.push },
          options.processKey,
        );
        // The deadline (timeoutMs) is the sandbox layer's: execute() reports it as a timeout
        // and calls terminate (sandbox/exec.ts).
        const exit = started.exit.finally(() => {
          stdout.end();
          stderr.end();
        });
        // Read after both streams end; a transport failure mid-stream rejects it then.
        exit.catch(() => undefined);
        return ok({
          exit_code: exit,
          stdout: stdout.chunks,
          stderr: stderr.chunks,
        });
      },
      unavailable,
    );

  const upload: SandboxSession["upload"] = (path, data, context) =>
    guarded<void, FileFailure>(
      context,
      async () => {
        const at = sandboxPath(path);
        if (at === undefined)
          return err({ code: "invalid_path", message: path });
        const refused = await checked(driver, id, checkWriteScript(at), path);
        if (refused !== undefined) return err(refused);
        await driver.write(id, at, data);
        return ok(undefined);
      },
      unavailable,
    );

  const download: SandboxSession["download"] = (path, context) =>
    guarded<Uint8Array, FileFailure>(
      context,
      async () => {
        const at = sandboxPath(path);
        if (at === undefined)
          return err({ code: "invalid_path", message: path });
        const refused = await checked(driver, id, checkReadScript(at), path);
        return refused === undefined
          ? ok(await driver.read(id, at))
          : err(refused);
      },
      unavailable,
    );

  const capture = driver.snapshot;
  // Only a provider-owned whole-sandbox boundary makes a capture quiescent. The tree hashed
  // before and after must also match, or a writer ran around it (not_quiescent).
  const snapshot: SandboxSession["snapshot"] = (operationKey, context) =>
    guarded<SnapshotData, Failure<"not_quiescent" | "unavailable" | "timeout">>(
      context,
      async () => {
        if (capture === undefined || capture.quiescence === "unconfirmed")
          return err({
            code: "unavailable",
            message: `${provider} has no confirmed quiescent snapshot`,
          });
        const before = await treeHash(driver, id);
        const made = await capture.take(id, operationKey);
        const after = await treeHash(driver, id);
        if (before === undefined || before !== after) {
          await driver.deleteSnapshot(made.ref);
          return err({
            code: before === undefined ? "unavailable" : "not_quiescent",
            message: `the tree of ${id} changed around the capture`,
          });
        }
        // The boundary covered the whole sandbox: every process in it was frozen, or ended.
        const whole = [id];
        return ok({
          snapshot_id: SnapshotId.parse(made.ref),
          provider,
          sandbox_id: sandboxId,
          capture_class: "filesystem",
          expires_at: made.expiresAt,
          manifest_hash: before,
          quiesced:
            capture.quiescence === "paused"
              ? { frozen: whole, stopped: [], excluded: [] }
              : { frozen: [], stopped: whole, excluded: [] },
        });
      },
      unavailable,
    );

  const close: SandboxSession["close"] = (context) =>
    guarded<void, ReleaseFailure>(
      context,
      async () => {
        await driver.kill(id);
        return ok(undefined);
      },
      (error) => ({ code: "release_failed", message: messageOf(error) }),
    );

  return {
    id: sandboxId,
    exec,
    terminate,
    upload,
    download,
    snapshot,
    close,
  };
}
