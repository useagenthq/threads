import { existsSync, mkdirSync, realpathSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { ConfigError } from "../../agent/errors";
import { sha256Hex } from "../../hash";
import { err, ok } from "../../result";
import type {
  Failure,
  ReleaseFailure,
  RestoreFailure,
  Sandbox,
  SandboxInfo,
  SandboxSession,
} from "../protocol";
import { fenceHere, guarded, messageOf } from "../remote/fence";
import { type Confinement, confinement, probe } from "./confine";
import { devSession } from "./session";

// devSandbox(): a local sandbox in a directory on the host, inside the platform's OS
// confinement (confine.ts). What it declares, and why:
//
// - Confinement is required. Without one, setup fails capability_missing naming what is
//   missing; a command never runs unconfined.
// - Create: the directory is named by the operation key, so a lost create answer is settled by
//   looking for that exact directory. Nothing else can have made it, so lookup is final.
// - Termination: killing the process group proves nothing about a descendant that left it, so
//   termination is unconfirmed and a crashed exec parks.
// - Snapshots: none. capture_classes is empty, snapshot is unavailable, and restore is
//   snapshot_missing.
// - Egress: denied by the confinement (no network namespace on Linux, `(deny network*)` on
//   macOS); allowInternet lifts it and the adapter declares egress unenforced.
// - Credentials: a command gets exactly the env of its call and nothing of the host's
//   (invariant 4), and the confinement gives it no home and no /run.

export type DevSandboxOptions = {
  /** Where sandbox directories are made. Defaults to threads-dev in the host's temp dir. */
  readonly root?: string;
  /** Lifts the confinement's network block (egress unenforced). Defaults to false. */
  readonly allowInternet?: boolean;
  /** The confinement program. Defaults to bwrap on Linux and sandbox-exec on macOS. */
  readonly tool?: string;
};

const PREFIX = "threads-dev-";

/** The directory one operation key owns: the same key always names the same one. */
function nameOf(operationKey: string): string {
  return `${PREFIX}${sha256Hex(operationKey).slice(0, 32)}`;
}

function infoOf(allowInternet: boolean): SandboxInfo {
  return {
    provider: "dev",
    egress: allowInternet ? "unenforced" : "enforced",
    capture_classes: [],
    browser: "none",
    desktop: "none",
    lookup: { create: "final", snapshot: "none" },
    termination: "unconfirmed",
  };
}

/** The dev root as it really is, with the confinement proven over it. */
type Ready = { readonly confined: Confinement; readonly root: string };

export function devSandbox(options: DevSandboxOptions = {}): Sandbox {
  const given = options.root ?? join(tmpdir(), "threads-dev");
  const allowInternet = options.allowInternet ?? false;
  let made: Ready | undefined;

  const unavailable = (error: unknown) =>
    ({ code: "unavailable", message: messageOf(error) }) as const;

  /**
   * The confinement and the dev root, proven at setup; a call before setup proves it then.
   * The root is resolved through its own symlinks once, because macOS's profile and the host
   * paths must name the same directory (its temp dir is reached through a symlink).
   */
  const ready = (): Ready => {
    if (made !== undefined) return made;
    const confined = confinement(allowInternet, options.tool);
    if (!confined.ok)
      throw new ConfigError("capability_missing", confined.error);
    mkdirSync(given, { recursive: true });
    const root = realpathSync(given);
    const missing = probe(confined.value, root);
    if (missing !== undefined)
      throw new ConfigError(
        "capability_missing",
        `devSandbox() needs an OS confinement: ${missing}`,
      );
    made = { confined: confined.value, root };
    return made;
  };

  const session = (name: string): SandboxSession => {
    const { confined, root } = ready();
    return devSession(join(root, name), name, confined);
  };

  return {
    info: infoOf(allowInternet),
    setup: async () => {
      ready();
    },
    create: (operationKey, context) =>
      guarded<SandboxSession, Failure<"unavailable" | "timeout">>(
        context,
        async () => {
          const name = nameOf(operationKey);
          const at = join(ready().root, name);
          await fenceHere();
          // One key, one directory: a second create of the same key finds its own.
          try {
            mkdirSync(at, { recursive: false, mode: 0o700 });
          } catch (error) {
            if (!existsSync(at)) return err(unavailable(error));
          }
          return ok(session(name));
        },
        unavailable,
      ),
    restore: (_snapshotId, _manifestHash, _operationKey, context) =>
      guarded<SandboxSession, RestoreFailure>(
        context,
        async () =>
          err({
            code: "snapshot_missing",
            message: "the dev sandbox has no snapshots",
          }),
        unavailable,
      ),
    lookup: async (operationKey, context) => {
      const fenced = await context.fence();
      if (!fenced.ok) return fenced;
      const name = nameOf(operationKey);
      // Final: only this key's create can have made this directory.
      return ok(
        existsSync(join(ready().root, name))
          ? { status: "found", value: session(name) }
          : { status: "not_found" },
      );
    },
    attach: (ref, context) =>
      guarded<
        SandboxSession,
        Failure<"not_found" | "resource_unknown" | "unavailable">
      >(
        context,
        async () => {
          if (!ref.startsWith(PREFIX) || ref.includes("/"))
            return err({
              code: "resource_unknown",
              message: `not a dev sandbox: ${ref}`,
            });
          await fenceHere();
          return existsSync(join(ready().root, ref))
            ? ok(session(ref))
            : err({ code: "not_found", message: `no live sandbox ${ref}` });
        },
        unavailable,
      ),
    release: (ref, context) =>
      guarded<"released" | "already_gone", ReleaseFailure>(
        context,
        async () =>
          err({
            code: "unavailable",
            message: `the dev sandbox has no snapshots (${ref})`,
          }),
        (error) => ({ code: "release_failed", message: messageOf(error) }),
      ),
  };
}
