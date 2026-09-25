import { SandboxId, SnapshotId } from "../../log";
import { err, ok } from "../../result";
import { admitExec } from "../admit";
import { isRefusal } from "../context";
import type {
  ExecOutput,
  Failure,
  FileFailure,
  ReleaseFailure,
  SandboxContext,
  SandboxSession,
  SnapshotData,
  Trees,
} from "../protocol";
import { type ReadFailure, treeHash } from "../trees";
import { byteStream, joined } from "./bytes";
import type { SandboxDriver } from "./driver";
import { FenceRefused, guarded, messageOf } from "./fence";
import {
  checkReadScript,
  checkWriteScript,
  EXPORT_TREE_SCRIPT,
  execScript,
  FILE_EXIT,
  importTreeScript,
  sandboxPath,
  stdinPath,
  treePath,
  WORKSPACE,
} from "./scripts";

// One remote sandbox as a SandboxSession (spec/api.json): every operation fences through the
// driver's transport, and everything above the raw provider calls is the shared POSIX kit.

/** A kit script's exit code and output, collected: its outputs are small. */
export async function runScript(
  driver: SandboxDriver,
  id: string,
  script: string,
): Promise<{
  readonly exit: number;
  readonly stdout: Uint8Array;
  readonly stderr: string;
}> {
  const out = byteStream();
  const err = byteStream();
  const started = await driver.run(id, script, {
    stdout: out.push,
    stderr: err.push,
  });
  const stdout = joined(out.chunks);
  const stderr = joined(err.chunks);
  const exit = await started.exit.finally(() => {
    out.end();
    err.end();
  });
  return {
    exit,
    stdout: await stdout,
    stderr: new TextDecoder().decode(await stderr),
  };
}

/**
 * The manifest hash of the sandbox's /workspace through its export, inside a kit operation:
 * undefined when it can't be read, and a refused fence thrown so the operation reports it.
 */
export async function measured(
  session: Pick<Trees, "exportTree">,
  context: SandboxContext,
): Promise<string | undefined> {
  const hash = await treeHash(session, context);
  if (hash.ok) return hash.value;
  if (isRefusal<ReadFailure["code"]>(hash.error))
    throw new FenceRefused(hash.error);
  return undefined;
}

/** A script started with its output streamed, never joined (exec, the tree export). */
async function streamed(
  driver: SandboxDriver,
  id: string,
  script: string,
  processKey?: string,
): Promise<ExecOutput> {
  const stdout = byteStream();
  const stderr = byteStream();
  const started = await driver.run(
    id,
    script,
    { stdout: stdout.push, stderr: stderr.push },
    processKey,
  );
  const exit = started.exit.finally(() => {
    stdout.end();
    stderr.end();
  });
  // Read after both streams end; a transport failure mid-stream rejects it then.
  exit.catch(() => undefined);
  return { exit_code: exit, stdout: stdout.chunks, stderr: stderr.chunks };
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
): SandboxSession & Trees {
  const sandboxId = SandboxId.parse(id);
  // A confirmed driver's answer. Otherwise a best-effort kill that never claims it worked: a
  // descendant can drop out of the process the provider tracks, so only an operator can
  // settle it (termination: unconfirmed).
  const terminate: SandboxSession["terminate"] = (processKey, context) =>
    guarded<
      "terminated" | "already_exited" | "unknown",
      Failure<"unavailable">
    >(
      context,
      async () => {
        if (driver.termination === "confirmed")
          return ok(await driver.terminate(id, processKey));
        await driver.stopProcess(id, processKey);
        return ok("unknown");
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
        if (options.stdin !== undefined)
          await driver.write(id, stdinPath(options.processKey), options.stdin);
        const script = execScript({
          command,
          cwd,
          env: options.env,
          processKey: options.processKey,
          stdin: options.stdin !== undefined,
        });
        // The deadline (timeoutMs) is the sandbox layer's: execute() reports it as a timeout
        // and calls terminate (sandbox/exec.ts).
        return ok(await streamed(driver, id, script, options.processKey));
      },
      unavailable,
    );
  };

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

  const exportTree: Trees["exportTree"] = (context) =>
    guarded<ExecOutput, Failure<"unavailable">>(
      context,
      async () => ok(await streamed(driver, id, EXPORT_TREE_SCRIPT)),
      unavailable,
    );

  // Uploaded as one file, then extracted by the sandbox's own tar (spec/schema/README.md,
  // "Sandbox image"); the builder already masked every mode to 0o777.
  const importTree: Trees["importTree"] = (tar, context) =>
    guarded<void, Failure<"unavailable">>(
      context,
      async () => {
        const path = treePath();
        await driver.write(id, path, await joined(tar));
        const run = await runScript(driver, id, importTreeScript(path));
        return run.exit === 0
          ? ok(undefined)
          : err({
              code: "unavailable",
              message: `the import into ${WORKSPACE} exited ${run.exit}: ${run.stderr}`,
            });
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
        const before = await measured({ exportTree }, context);
        const made = await capture.take(id, operationKey);
        const after = await measured({ exportTree }, context);
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
    exportTree,
    importTree,
  };
}
