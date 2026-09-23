import type { Fetch, SandboxDriver } from "@threads/core/adapter";
import {
  CommandExitError,
  type CommandHandle,
  NotFoundError,
  Sandbox,
  SandboxError,
} from "e2b";
import { through } from "./transport";

// The remote kit's driver over the E2B SDK. The API key is used only to authenticate the
// host's own requests; a sandbox is created with no env vars, and nothing from the host env
// reaches it (AGENTS invariant 4).

/** The metadata key a create is found by. */
export const OPERATION_KEY = "threads_operation_key";
/** The env var E2B's process table records an exec under, so a stop can find it later. */
export const PROCESS_KEY = "THREADS_PROCESS_KEY";

export type Connection = {
  readonly apiKey?: string;
  readonly domain?: string;
};

export type DriverOptions = {
  readonly connection: Connection;
  readonly template: string;
  readonly timeoutMs: number;
  readonly internet: boolean;
  readonly fetch: Fetch;
};

const utf8 = new TextEncoder();

async function exitOf(handle: CommandHandle): Promise<number> {
  try {
    return (await handle.wait()).exitCode;
  } catch (error) {
    if (error instanceof CommandExitError) return error.exitCode;
    throw error;
  }
}

export function e2bDriver(options: DriverOptions): SandboxDriver {
  const { connection } = options;
  const send = <T>(call: () => Promise<T>): Promise<T> =>
    through(options.fetch, call);
  const boxes = new Map<string, Sandbox>();
  /** A handle on a live sandbox; after a restart, by connecting to it again. */
  const box = async (id: string): Promise<Sandbox> => {
    const known = boxes.get(id);
    if (known !== undefined) return known;
    const connected = await Sandbox.connect(id, connection);
    boxes.set(id, connected);
    return connected;
  };

  return {
    create: (key, snapshot) =>
      send(async () => {
        try {
          const made = await Sandbox.create(snapshot ?? options.template, {
            ...connection,
            metadata: { [OPERATION_KEY]: key },
            envs: {},
            timeoutMs: options.timeoutMs,
            allowInternetAccess: options.internet,
          });
          boxes.set(made.sandboxId, made);
          return { kind: "created", id: made.sandboxId };
        } catch (error) {
          // E2B answers a create from an unknown snapshot or template with a 404.
          const unknown =
            error instanceof SandboxError && error.statusCode === 404;
          if (snapshot === undefined || !unknown) throw error;
          return { kind: "snapshot_missing", message: error.message };
        }
      }),
    find: (key) =>
      send(async () => {
        const page = await Sandbox.list({
          ...connection,
          query: {
            metadata: { [OPERATION_KEY]: key },
            state: ["running", "paused"],
          },
        }).nextItems();
        const hit = page[0];
        return hit === undefined
          ? { status: "not_found_nonfinal" }
          : { status: "found", value: hit.sandboxId };
      }),
    exists: (id) =>
      send(async () => {
        try {
          await Sandbox.getInfo(id, connection);
          return true;
        } catch (error) {
          if (error instanceof NotFoundError) return false;
          throw error;
        }
      }),
    kill: (id) =>
      send(async () => {
        boxes.delete(id);
        return (await Sandbox.kill(id, connection)) ? "killed" : "already_gone";
      }),
    run: (id, script, sinks, processKey) =>
      send(async () => {
        const handle = await (await box(id)).commands.run(script, {
          background: true,
          user: "root",
          cwd: "/",
          envs: processKey === undefined ? {} : { [PROCESS_KEY]: processKey },
          timeoutMs: 0,
          onStdout: (text) => sinks.stdout(utf8.encode(text)),
          onStderr: (text) => sinks.stderr(utf8.encode(text)),
        });
        return { exit: exitOf(handle) };
      }),
    stopProcess: (id, processKey) =>
      send(async () => {
        const sandbox = await box(id);
        for (const process of await sandbox.commands.list())
          if (process.envs[PROCESS_KEY] === processKey)
            await sandbox.commands.kill(process.pid);
      }),
    write: (id, path, data) =>
      send(async () => {
        await (await box(id)).files.write(path, data.slice().buffer, {
          user: "root",
          useOctetStream: true,
        });
      }),
    read: (id, path) =>
      send(async () =>
        (await box(id)).files.read(path, { format: "bytes", user: "root" }),
      ),
    snapshot: {
      // createSnapshot pauses the sandbox for the capture, but E2B doesn't document that the
      // pause is a barrier for every descendant and clone, so quiescence stays unconfirmed
      // (no snapshots are taken) until a live qualification proves it. A restore
      // from an E2B snapshot id still works, verified against the manifest hash.
      quiescence: "unconfirmed",
      take: (id, key) =>
        send(async () => {
          const made = await (await box(id)).createSnapshot({
            name: `threads-${key}`,
          });
          // E2B keeps a snapshot until it is deleted.
          return { ref: made.snapshotId, expiresAt: null };
        }),
    },
    deleteSnapshot: (ref) =>
      send(async () =>
        (await Sandbox.deleteSnapshot(ref, connection))
          ? "released"
          : "already_gone",
      ),
  };
}
