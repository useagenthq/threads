import { createHash } from "node:crypto";
import { setTimeout as sleep } from "node:timers/promises";
import type { Sandbox as SandboxDto } from "@daytona/api-client";
import {
  type BestEffortStop,
  quote,
  type SandboxDriver,
} from "@threads/core/adapter";
import { type Clients, statusOf } from "./clients";
import { framed, unhex } from "./framing";
import { follow, type OpenSocket } from "./logs";

// The remote kit's driver over Daytona's API clients. A sandbox is named by its operation key,
// so a lost create is found by name. Every wait polls the control plane, each poll fenced.

export type DriverOptions = {
  /** Made on first use, after setup resolved the key. */
  readonly clients: () => Clients;
  readonly open: OpenSocket;
  /** The Daytona snapshot new sandboxes start from; undefined: Daytona's default. */
  readonly snapshot: string | undefined;
  /** The region sandboxes are created in; undefined: the organization's default. */
  readonly target: string | undefined;
  readonly ttlMinutes: number;
  /** Idle minutes before Daytona stops a sandbox; a stopped one is deleted after as long. */
  readonly autoStopMinutes: number;
  readonly networkBlockAll: boolean;
  readonly pollMs: number;
  readonly waitMs: number;
};

/** The sandbox and snapshot name an operation key maps to. */
export const nameOf = (operationKey: string): string =>
  `threads-${operationKey}`;

const GONE = new Set(["destroyed", "destroying"]);
const FAILED = new Set(["error", "build_failed"]);
const CONFLICT = 409;

/**
 * Daytona's toolbox runs as the image's user (`daytona`, not root, whatever `user` says), and /
 * is root's, so /workspace is made with the image's passwordless sudo and handed to that user.
 */
const PREPARE_WORKSPACE = [
  ": threads-daytona-workspace",
  "mkdir -p /workspace 2>/dev/null",
  '[ -w /workspace ] || { sudo -n mkdir -p /workspace && sudo -n chown "$(id -u):$(id -g)" /workspace; }',
].join("\n");

const quiet = { stdout: () => undefined, stderr: () => undefined };

/** Runs `call`, reading a 404 as undefined. */
async function unless404<T>(call: () => Promise<T>): Promise<T | undefined> {
  try {
    return await call();
  } catch (error) {
    if (statusOf(error) === 404) return undefined;
    throw error;
  }
}

export function daytonaDriver(options: DriverOptions): SandboxDriver {
  const sandboxes = () => options.clients().sandboxes;
  const snapshots = () => options.clients().snapshots;
  const proxies = new Map<string, string>();
  const exec = sessions(options, (id) => toolbox(id));

  const get = (id: string) =>
    unless404(async () => (await sandboxes().getSandbox(id)).data);

  /** Polls until the sandbox reaches `state` (undefined: it is gone). */
  const until = async (id: string, state: string | undefined) => {
    const deadline = Date.now() + options.waitMs;
    while (Date.now() < deadline) {
      const now = (await get(id))?.state;
      if (
        state === undefined ? now === undefined || GONE.has(now) : now === state
      )
        return;
      if (now !== undefined && FAILED.has(now))
        throw new Error(`sandbox ${id} is in state ${now}`);
      await sleep(options.pollMs);
    }
    throw new Error(`sandbox ${id} didn't reach ${state ?? "destroyed"}`);
  };

  const remember = (dto: SandboxDto) => {
    proxies.set(dto.id, `${dto.toolboxProxyUrl.replace(/\/$/, "")}/${dto.id}`);
    return dto.id;
  };

  /** The toolbox of a sandbox, at the proxy URL its record gives. */
  const toolbox = async (id: string) => {
    const known = proxies.get(id);
    if (known !== undefined)
      return { base: known, ...options.clients().toolbox(known) };
    const dto = await get(id);
    if (dto === undefined) throw new Error(`no sandbox ${id}`);
    const base = proxies.get(remember(dto)) ?? "";
    return { base, ...options.clients().toolbox(base) };
  };

  const snapshotReady = async (name: string) => {
    const deadline = Date.now() + options.waitMs;
    while (Date.now() < deadline) {
      const state = (
        await unless404(async () => (await snapshots().getSnapshot(name)).data)
      )?.state;
      if (state === "active") return;
      if (state === "error" || state === "build_failed")
        throw new Error(`snapshot ${name} failed: ${state}`);
      await sleep(options.pollMs);
    }
    throw new Error(`snapshot ${name} didn't become active`);
  };

  /** Creates the key's sandbox; a name is unique, so a 409 is this key's earlier create. */
  const post = async (key: string, snapshot: string | undefined) => {
    try {
      return await unless404(
        async () =>
          (
            await sandboxes().createSandbox({
              name: nameOf(key),
              ...(snapshot === undefined ? {} : { snapshot }),
              ...(options.target === undefined
                ? {}
                : { target: options.target }),
              env: {},
              labels: { threads_operation_key: key },
              user: "root",
              public: false,
              networkBlockAll: options.networkBlockAll,
              // The safety net for a leak the ledger misses: an idle sandbox stops, then goes.
              autoStopInterval: options.autoStopMinutes,
              autoDeleteInterval: options.autoStopMinutes,
              ttlMinutes: options.ttlMinutes,
            })
          ).data,
      );
    } catch (error) {
      if (statusOf(error) !== CONFLICT) throw error;
      const found = await get(nameOf(key));
      if (found === undefined) throw error;
      return found;
    }
  };

  return {
    create: async (key, snapshot) => {
      const made = await post(key, snapshot ?? options.snapshot);
      if (made === undefined)
        return {
          kind: "snapshot_missing",
          message: `no Daytona snapshot ${snapshot}`,
        };
      const id = remember(made);
      await until(id, "started");
      const prepared = await (
        await exec.run(id, PREPARE_WORKSPACE, quiet, undefined)
      ).exit;
      if (prepared !== 0)
        throw new Error(
          `sandbox ${id} can't make /workspace (exit ${prepared})`,
        );
      return { kind: "created", id };
    },
    find: async (key) => {
      const dto = await get(nameOf(key));
      return dto === undefined || GONE.has(dto.state ?? "")
        ? { status: "not_found_nonfinal" }
        : { status: "found", value: remember(dto) };
    },
    exists: async (id) => {
      const dto = await get(id);
      return dto !== undefined && !GONE.has(dto.state ?? "");
    },
    kill: async (id) => {
      proxies.delete(id);
      const deleted = await unless404(() => sandboxes().deleteSandbox(id));
      if (deleted === undefined) return "already_gone";
      // Daytona deletes asynchronously; the kill counts once it is destroyed.
      await until(id, undefined);
      return "killed";
    },
    ...exec,
    snapshot: {
      // A cold snapshot: Daytona documents a whole-process pause only for Linux VM and Windows
      // sandboxes, and a live snapshot of a container can't be proven quiescent. A stop is a
      // provider-owned boundary for every class, so the parent is stopped (its processes end:
      // disruptive), captured, and started again.
      quiescence: "stopped",
      take: async (id, key) => {
        const name = nameOf(key);
        await sandboxes().stopSandbox(id);
        await until(id, "stopped");
        try {
          await sandboxes().createSandboxSnapshot(id, { name });
          await until(id, "stopped");
          await snapshotReady(name);
        } finally {
          proxies.delete(id);
          await sandboxes().startSandbox(id);
          await until(id, "started");
        }
        // Daytona keeps a snapshot until it is removed.
        return { ref: name, expiresAt: null };
      },
    },
    deleteSnapshot: async (ref) => {
      const dto = await unless404(
        async () => (await snapshots().getSnapshot(ref)).data,
      );
      if (dto === undefined) return "already_gone";
      return (await unless404(() => snapshots().removeSnapshot(dto.id))) ===
        undefined
        ? "already_gone"
        : "released";
    },
  };
}

type Toolbox = (id: string) => Promise<{
  readonly base: string;
  readonly process: ReturnType<Clients["toolbox"]>["process"];
  readonly files: ReturnType<Clients["toolbox"]>["files"];
}>;

/** A session per process key, so a later stop can end it by that key. */
const sessionOf = (processKey: string): string =>
  `threads-${createHash("sha256").update(processKey).digest("hex").slice(0, 32)}`;

function sessions(
  options: DriverOptions,
  toolbox: Toolbox,
): Pick<SandboxDriver, "run" | "write" | "read"> & BestEffortStop {
  const exitOf = async (
    box: Awaited<ReturnType<Toolbox>>,
    session: string,
    command: string,
  ): Promise<number> => {
    const deadline = Date.now() + options.waitMs;
    while (Date.now() < deadline) {
      // A running command's exitCode is null on the wire, despite the client's type.
      const exit: unknown = (
        await box.process.getSessionCommand(session, command)
      ).data.exitCode;
      if (typeof exit === "number") return exit;
      await sleep(options.pollMs);
    }
    throw new Error(`command ${command} reported no exit code`);
  };
  return {
    run: async (id, script, sinks, processKey) => {
      const box = await toolbox(id);
      const session =
        processKey === undefined
          ? `threads-${crypto.randomUUID()}`
          : sessionOf(processKey);
      await box.process.createSession({ sessionId: session });
      const { cmdId } = (
        await box.process.sessionExecuteCommand(session, {
          command: `sh -c ${quote(framed(script))}`,
          runAsync: true,
        })
      ).data;
      const url = `${box.base.replace(/^http/, "ws")}/process/session/${session}/command/${cmdId}/logs?follow=true`;
      const finish = async () => {
        await follow(options.open, url, options.clients().headers, {
          stdout: unhex(sinks.stdout).push,
          stderr: unhex(sinks.stderr).push,
        });
        const exit = await exitOf(box, session, cmdId);
        await unless404(() => box.process.deleteSession(session));
        return exit;
      };
      return { exit: finish() };
    },
    stopProcess: async (id, processKey) => {
      const box = await toolbox(id);
      await unless404(() => box.process.deleteSession(sessionOf(processKey)));
    },
    write: async (id, path, data) => {
      const box = await toolbox(id);
      await box.files.uploadFile(path, new File([data.slice()], "file"));
    },
    read: async (id, path) => {
      const box = await toolbox(id);
      const res = await box.files.downloadFile(path, {
        responseType: "arraybuffer",
      });
      const body: unknown = res.data;
      if (!(body instanceof ArrayBuffer))
        throw new Error("a download that isn't bytes");
      return new Uint8Array(body);
    },
  };
}
