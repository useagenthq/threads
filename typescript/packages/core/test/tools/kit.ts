import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";
import { JsonObject, type KnownEvent, SandboxId } from "../../src/log";
import type { ToolContext, ToolImpl, ToolRun } from "../../src/loop/types";
import { err, ok } from "../../src/result";
import type { SandboxSession } from "../../src/sandbox/protocol";
import { type ArtifactStore, memoryArtifacts } from "../../src/store/artifacts";
import type { Builtin, BuiltinEnv } from "../../src/tools/builtin";
import { CTX } from "../sandbox/context";
import { ROOT } from "../store/helpers";

// Test kit for the gated built-ins: a sandbox session backed by a local directory (the sandbox
// paths /workspace and /tmp map under `root`, and commands really run), and a bound tool.

export type LocalSession = SandboxSession & {
  /** Every exec's argv (as the tool sent it) and env. */
  readonly execs: {
    readonly argv: readonly string[];
    readonly env: Readonly<Record<string, string>>;
  }[];
};

export function localSession(root: string): LocalSession {
  // One pass that leaves the test's host paths alone: on Linux they are under /tmp/ too.
  const host = dirname(root);
  const paths = new RegExp(`${RegExp.escape(host)}|/workspace|/tmp/`, "g");
  const map = (s: string): string =>
    s.replaceAll(paths, (m) => (m === host ? m : `${root}${m}`));
  mkdirSync(`${root}/workspace`, { recursive: true });
  mkdirSync(`${root}/tmp`, { recursive: true });
  const execs: LocalSession["execs"] = [];
  return {
    id: SandboxId.parse("local"),
    execs,
    exec: async (command, _ctx, options) => {
      execs.push({ argv: command, env: options.env ?? {} });
      // The tool's env exactly, plus PATH so the host's binaries resolve.
      const proc = Bun.spawn(command.map(map), {
        cwd: map(options.cwd ?? "/workspace"),
        env: {
          ...options.env,
          PATH: process.env["PATH"] ?? "/usr/bin:/bin",
          HOME: root,
        },
        stdout: "pipe",
        stderr: "pipe",
      });
      return ok({
        exit_code: proc.exited,
        stdout: proc.stdout,
        stderr: proc.stderr,
      });
    },
    terminate: async () => ok("unknown"),
    upload: async (path, data) => {
      mkdirSync(dirname(map(path)), { recursive: true });
      writeFileSync(map(path), data);
      return ok(undefined);
    },
    download: async (path) => {
      try {
        return ok(new Uint8Array(readFileSync(map(path))));
      } catch {
        return err({ code: "not_found", message: path });
      }
    },
    snapshot: async () => err({ code: "unavailable", message: "local" }),
    close: async () => ok(undefined),
  };
}

export type Bound = {
  readonly impl: ToolImpl;
  readonly artifacts: ArtifactStore;
  readonly run: (
    input: Record<string, unknown>,
    ctx?: Partial<ToolContext>,
  ) => Promise<ToolRun>;
};

/** A built-in bound to a session (or none), with an in-memory artifact store. */
export function bound(
  b: Builtin,
  session?: SandboxSession,
  events: () => readonly KnownEvent[] = () => [],
): Bound {
  const artifacts = memoryArtifacts();
  const env: BuiltinEnv = {
    session: async () =>
      session === undefined
        ? err({ code: "unavailable", message: "no sandbox" })
        : ok(session),
    context: CTX,
    artifacts,
    events,
  };
  const impl = b.bind(env);
  const run = (
    input: Record<string, unknown>,
    ctx: Partial<ToolContext> = {},
  ) =>
    impl.run(JsonObject.parse(input), {
      effectKey: `${ROOT}:c1`,
      callId: "c1",
      branchId: ROOT,
      epoch: 1,
      principal: { issuer: "api", tenant: "local", subject: "operator" },
      signal: new AbortController().signal,
      fence: async () => ok(undefined),
      ...ctx,
    });
  return { impl, artifacts, run };
}
