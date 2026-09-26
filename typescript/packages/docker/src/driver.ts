import {
  fenceHere,
  type SandboxDriver,
  type Sinks,
  type Started,
} from "@threads/core/adapter";
import { z } from "zod";
import { createContainer, type Limits } from "./create";
import { type Engine, failure } from "./engine";
import { type ExecSpec, startExec } from "./exec";
import { OPERATION_KEY, shortHash, volumesOf } from "./names";
import type { Supervisor } from "./pins";
import { commandExit, terminate } from "./records";
import { onlyFile, tarOf } from "./tar";
import { DockerError, unavailable } from "./wire";

// The remote kit's driver over the Docker Engine API. A command with a process key runs
// through the supervisor as uid 0, which admits one at a time and can prove it killed the
// group; every other script (the kit's file checks, the tree export and import) runs as uid
// 1000 with no lock. Nothing from the host env reaches a container (AGENTS invariant 4).

const Listed = z.array(z.object({ Names: z.array(z.string()) }));

/** The supervisor's own cap. driver.run is never told the call's timeout; execute() owns it. */
const DEADLINE_CAP_MS = 3_600_000;
/** Enough of stderr to recognise the supervisor's admission refusal. */
const MARKER_BYTES = 200;

export function dockerDriver(
  engine: Engine,
  limits: Limits,
  supervisor: Supervisor,
): SandboxDriver {
  const path = (name: string) => `/containers/${encodeURIComponent(name)}`;

  /** An archive PUT of one file into the volume that holds it. */
  const put = async (
    id: string,
    into: string,
    name: string,
    mode: number,
    data: Uint8Array,
  ): Promise<void> => {
    const res = await engine.send(
      "PUT",
      `${path(id)}/archive?path=${encodeURIComponent(into)}`,
      { tar: tarOf([{ name, mode, uid: 1000, gid: 1000, bytes: data }]) },
    );
    if (!res.ok) throw await failure(res, `the write of ${into}/${name}`);
    await res.text();
  };

  const run = async (
    id: string,
    script: string,
    sinks: Sinks,
    processKey?: string,
  ): Promise<Started> => {
    const spec: ExecSpec =
      processKey === undefined
        ? { user: "1000", cmd: ["/bin/sh", "-c", script] }
        : {
            user: "0",
            cmd: [
              "/run/threads/bin/supervise",
              shortHash(processKey),
              String(DEADLINE_CAP_MS),
              "/bin/sh",
              "-c",
              script,
            ],
          };
    if (processKey === undefined)
      return startExec(engine, id, spec, sinks, () => {
        throw unavailable("Docker reported no exit code for the command");
      });
    // The supervisor's own stderr is what says it refused admission; the command's exit
    // code is in the record it wrote, not in the exec's status.
    let marker = "";
    const watched: Sinks = {
      stdout: sinks.stdout,
      stderr: (chunk) => {
        if (marker.length < MARKER_BYTES)
          marker += new TextDecoder().decode(chunk);
        sinks.stderr(chunk);
      },
    };
    const hash = shortHash(processKey);
    const started = await startExec(engine, id, spec, watched, () => 0);
    return {
      exit: started.exit.then((status) =>
        commandExit(engine, id, hash, status, marker),
      ),
    };
  };

  return {
    termination: "confirmed",
    create: async (operationKey, snapshot) => {
      // ponytail: no driver snapshot until 16C's host trees land; `docker commit` would miss
      // the /workspace volume, so there is nothing honest to restore from here.
      if (snapshot !== undefined)
        return {
          kind: "snapshot_missing",
          message: "docker has no snapshots",
        };
      return {
        kind: "created",
        id: await createContainer(engine, operationKey, limits, supervisor),
      };
    },
    find: async (operationKey) => {
      const filters = JSON.stringify({
        label: [`${OPERATION_KEY}=${operationKey}`],
      });
      const found = await engine.json(
        Listed,
        "a container listing",
        "GET",
        `/containers/json?all=1&filters=${encodeURIComponent(filters)}`,
      );
      const names = found.flatMap((c) =>
        c.Names.map((n) => (n.startsWith("/") ? n.slice(1) : n)),
      );
      const [only, ...more] = names;
      if (only === undefined) return { status: "not_found_nonfinal" };
      if (more.length > 0)
        return {
          status: "unknown",
          reason: `several containers carry ${operationKey}: ${names.join(", ")}`,
        };
      return { status: "found", value: only };
    },
    exists: async (id) => (await engine.inspect(id)) !== undefined,
    kill: async (id) => {
      const res = await engine.send("DELETE", `${path(id)}?force=1&v=1`);
      const gone = res.status === 404;
      if (!res.ok && !gone) throw await failure(res, "a container remove");
      await res.text();
      for (const volume of volumesOf(id)) {
        const dropped = await engine.send(
          "DELETE",
          `/volumes/${encodeURIComponent(volume)}`,
        );
        if (!dropped.ok && dropped.status !== 404)
          throw await failure(dropped, `the remove of volume ${volume}`);
        await dropped.text();
      }
      return gone ? "already_gone" : "killed";
    },
    run,
    terminate: (id, processKey) => terminate(engine, id, shortHash(processKey)),
    write: async (id, at, data) => {
      // Only a volume takes an archive PUT while the rootfs is read-only: /workspace is the
      // sandbox's tree, /tmp the kit's staging (stdin, an import's tar).
      for (const [root, mode] of [
        ["/workspace", 0o644],
        ["/tmp", 0o600],
      ] as const)
        if (at.startsWith(`${root}/`))
          return put(id, root, at.slice(root.length + 1), mode, data);
      throw unavailable(
        `a docker sandbox writes only under /workspace and /tmp, not ${at}`,
      );
    },
    read: async (id, at) => {
      const res = await engine.send(
        "GET",
        `${path(id)}/archive?path=${encodeURIComponent(at)}`,
      );
      if (res.status === 404) {
        await res.text();
        throw new DockerError("not_found", `no file ${at}`);
      }
      if (!res.ok) throw await failure(res, `the read of ${at}`);
      return onlyFile(new Uint8Array(await res.arrayBuffer()), at);
    },
    // No snapshot of this provider can exist, but the release still may not leave without
    // authority: the fence is checked here as it would be at a real send.
    deleteSnapshot: async () => {
      await fenceHere();
      return "already_gone";
    },
  };
}
