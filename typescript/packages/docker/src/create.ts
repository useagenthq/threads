import type { Sinks } from "@threads/core/adapter";
import { z } from "zod";
import { type Engine, failure } from "./engine";
import { startExec } from "./exec";
import { containerName, OPERATION_KEY, volumesOf } from "./names";
import { type Arch, supervisorBinary } from "./pins";
import { tarOf } from "./tar";
import { parsed, unavailable } from "./wire";

// The create sequence: resolve the image (pulling it anonymously when it is missing), refuse
// an architecture there is no supervisor for, create the container, prove the daemon applied
// every requested limit, inject the supervisor under its pinned sha256, start, and prove the
// image has the shell the kit's scripts need. No registry credentials are ever sent, and the
// request adds nothing to the environment but PATH (AGENTS invariant 4).

const Created = z.object({
  Id: z.string().min(1),
  Warnings: z.array(z.string()).nullish(),
});
const ImageInspected = z.object({ Architecture: z.string() });
const Tools = z.object({
  sh: z.boolean(),
  env: z.boolean(),
  bash: z.boolean(),
  tar: z.boolean(),
  git: z.boolean(),
  python3: z.boolean(),
});

/** Every process gets this PATH and nothing else; the image's own ENV reaches no command. */
const PATH =
  "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin";
export const PIDS_LIMIT = 1024;

export type Limits = {
  readonly image: string;
  readonly internet: boolean;
  readonly nanoCpus: number | undefined;
  readonly memoryBytes: number | undefined;
};

/** The limits a daemon may silently drop, by the word its warning uses. */
const DROPPABLE: readonly (readonly [RegExp, string])[] = [
  [/\bcpu\b/i, "CPU"],
  [/\bmemory\b/i, "memory"],
  [/\bswap\b/i, "swap"],
  [/\bpids\b/i, "pids"],
];

const cantEnforce = (limit: string) =>
  unavailable(
    `Docker can't enforce the ${limit} limit here (rootless Docker needs cgroup v2 delegation)`,
  );

export function createBody(
  name: string,
  operationKey: string,
  limits: Limits,
): Record<string, unknown> {
  const [exec, workspace, tmp] = volumesOf(name);
  return {
    Image: limits.image,
    Entrypoint: ["/run/threads/bin/supervise", "--idle"],
    Cmd: [],
    User: "0",
    Env: [PATH],
    Healthcheck: { Test: ["NONE"] },
    Labels: { [OPERATION_KEY]: operationKey },
    WorkingDir: "/workspace",
    HostConfig: {
      Init: true,
      ReadonlyRootfs: true,
      ...(limits.internet ? {} : { NetworkMode: "none" }),
      PidsLimit: PIDS_LIMIT,
      ...(limits.nanoCpus === undefined ? {} : { NanoCpus: limits.nanoCpus }),
      ...(limits.memoryBytes === undefined
        ? {}
        : { Memory: limits.memoryBytes, MemorySwap: limits.memoryBytes }),
      CapDrop: ["ALL"],
      // CHOWN is the supervisor's: --idle gives /workspace to uid 1000 once, at start.
      CapAdd: ["SETUID", "SETGID", "KILL", "CHOWN"],
      SecurityOpt: ["no-new-privileges"],
      Mounts: [
        { Type: "volume", Source: exec, Target: "/run/threads" },
        {
          Type: "volume",
          Source: workspace,
          Target: "/workspace",
          VolumeOptions: { NoCopy: true },
        },
        // /tmp is a tmpfs, but a HostConfig.Tmpfs is not a volume, and the daemon refuses an
        // archive PUT anywhere outside a volume while ReadonlyRootfs holds. The local driver
        // gives a tmpfs that *is* a volume, so the kit can still stage stdin and tree tars.
        {
          Type: "volume",
          Source: tmp,
          Target: "/tmp",
          VolumeOptions: {
            NoCopy: true,
            DriverConfig: {
              Name: "local",
              Options: {
                type: "tmpfs",
                device: "tmpfs",
                o: "uid=1000,gid=1000,mode=1777,nosuid,nodev,noexec",
              },
            },
          },
        },
      ],
    },
  };
}

/** The image's architecture, pulling it anonymously when the daemon doesn't have it. */
async function architecture(engine: Engine, image: string): Promise<Arch> {
  const at = `/images/${encodeURIComponent(image)}/json`;
  let res = await engine.send("GET", at);
  if (res.status === 404) {
    await res.text();
    await pull(engine, image);
    res = await engine.send("GET", at);
  }
  if (!res.ok) throw await failure(res, `the inspect of image ${image}`);
  const arch = parsed(
    ImageInspected,
    await res.text(),
    "an image inspect",
  ).Architecture;
  if (arch !== "amd64" && arch !== "arm64")
    throw unavailable(
      `docker: image ${image} is ${arch}; docker() needs linux/amd64 or linux/arm64`,
    );
  return arch;
}

/** An anonymous pull: no registry credentials are sent, so a private image must be local. */
async function pull(engine: Engine, image: string): Promise<void> {
  const res = await engine.send(
    "POST",
    `/images/create?fromImage=${encodeURIComponent(image)}`,
  );
  const body = await res.text();
  const last = body.trimEnd().split("\n").at(-1) ?? "";
  if (!res.ok || last.includes(`"error"`))
    throw unavailable(
      `image ${image} isn't available locally; run \`docker pull ${image}\``,
    );
}

/** Removes a container the create can't stand behind, with its volumes. */
async function remove(engine: Engine, name: string): Promise<void> {
  await (
    await engine.send(
      "DELETE",
      `/containers/${encodeURIComponent(name)}?force=1&v=1`,
    )
  ).text();
  for (const volume of volumesOf(name))
    await (
      await engine.send("DELETE", `/volumes/${encodeURIComponent(volume)}`)
    ).text();
}

/** A create's Warnings, and the applied config, must show every requested limit in force. */
async function enforced(
  engine: Engine,
  name: string,
  warnings: readonly string[],
  limits: Limits,
): Promise<void> {
  for (const warning of warnings)
    for (const [pattern, limit] of DROPPABLE)
      if (pattern.test(warning)) {
        await remove(engine, name);
        throw cantEnforce(limit);
      }
  const applied = await engine.inspect(name);
  if (applied === undefined) throw unavailable(`the container ${name} is gone`);
  const dropped = [
    [limits.nanoCpus, applied.nanoCpus, "CPU"],
    [limits.memoryBytes, applied.memory, "memory"],
    [PIDS_LIMIT, applied.pidsLimit, "pids"],
  ] as const;
  for (const [want, got, limit] of dropped)
    if (want !== undefined && got !== want) {
      await remove(engine, name);
      throw cantEnforce(limit);
    }
}

/** The archive injected into /run/threads: the supervisor, and the state directory it owns. */
export function supervisorArchive(arch: Arch): Uint8Array {
  return tarOf([
    { name: "bin", mode: 0o700, uid: 0, gid: 0 },
    {
      name: "bin/supervise",
      mode: 0o700,
      uid: 0,
      gid: 0,
      bytes: supervisorBinary(arch),
    },
    { name: "state", mode: 0o700, uid: 0, gid: 0 },
  ]);
}

/** `supervise --check`: the image must have sh and env for the kit's scripts to run. */
async function checkImage(
  engine: Engine,
  name: string,
  image: string,
): Promise<void> {
  const out: Uint8Array[] = [];
  const err: Uint8Array[] = [];
  const sinks: Sinks = {
    stdout: (b) => out.push(b),
    stderr: (b) => err.push(b),
  };
  const started = await startExec(
    engine,
    name,
    { user: "0", cmd: ["/run/threads/bin/supervise", "--check"] },
    sinks,
    () => 1,
  );
  const exit = await started.exit;
  const text = new TextDecoder().decode(
    Uint8Array.from(out.flatMap((c) => [...c])),
  );
  try {
    const tools =
      exit === 0 ? parsed(Tools, text, "the image check") : undefined;
    if (tools?.sh === true && tools.env === true) return;
  } catch (error) {
    // A probe whose answer can't be read leaves no container behind either.
    await remove(engine, name);
    throw error;
  }
  await remove(engine, name);
  throw unavailable(`image ${image} lacks /bin/sh (docker() needs sh and env)`);
}

/**
 * Creates the sandbox container for `operationKey` and returns its name. A 409 is this key's
 * own earlier create, so the name is used as it stands.
 */
export async function createContainer(
  engine: Engine,
  operationKey: string,
  limits: Limits,
): Promise<string> {
  const arch = await architecture(engine, limits.image);
  const name = containerName(operationKey);
  const res = await engine.send(
    "POST",
    `/containers/create?name=${encodeURIComponent(name)}`,
    { json: createBody(name, operationKey, limits) },
  );
  if (res.status === 409) await res.text();
  else {
    if (!res.ok) throw await failure(res, "a container create");
    const made = parsed(Created, await res.text(), "a container create");
    await enforced(engine, name, made.Warnings ?? [], limits);
  }
  const put = await engine.send(
    "PUT",
    `/containers/${encodeURIComponent(name)}/archive?path=${encodeURIComponent("/run/threads")}`,
    { tar: supervisorArchive(arch) },
  );
  if (!put.ok) throw await failure(put, "the supervisor injection");
  await put.text();
  const started = await engine.send(
    "POST",
    `/containers/${encodeURIComponent(name)}/start`,
  );
  if (!started.ok && started.status !== 304)
    throw await failure(started, "a container start");
  await started.text();
  await checkImage(engine, name, limits.image);
  return name;
}
