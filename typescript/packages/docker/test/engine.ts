import type { Fetch, Sinks } from "@threads/core/adapter";
import { z } from "zod";
import { sha256Hex } from "../../core/src/hash";
import type { Machine } from "../../core/test/sandbox/remote/machine";
import type { World } from "../../core/test/sandbox/remote/world";
import { OPERATION_KEY, volumesOf } from "../src/names";
import { tarOf, untar } from "../src/tar";

// The Docker Engine API mocked over a World: one emulated machine per container, plus the
// supervisor state (a generation and one record per key) the host reads back by archive GET.
// Every request is recorded, so the contract suite can prove nothing secret ever left.

const TOOLS = `{"sh":true,"env":true,"bash":true,"tar":true,"git":true,"python3":true}\n`;
const utf8 = new TextEncoder();

const json = (status: number, body: unknown): Response =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
const empty = (status: number): Response => new Response(null, { status });
const missing = (what: string): Response =>
  json(404, { message: `no ${what}` });

type Applied = {
  readonly NanoCpus?: number | undefined;
  readonly Memory?: number | undefined;
  readonly PidsLimit: number;
};

const CreateBody = z.object({
  Image: z.string(),
  Labels: z.record(z.string(), z.string()),
  HostConfig: z.object({
    NanoCpus: z.number().optional(),
    Memory: z.number().optional(),
    PidsLimit: z.number(),
  }),
});
const ExecBody = z.object({ User: z.string(), Cmd: z.array(z.string()) });

type Written = {
  readonly key: string;
  readonly generation: string;
  readonly state: "running" | "exited" | "terminated" | "stuck";
  readonly child_pid: number;
  readonly supervisor_pid: number;
  readonly supervisor_start: number;
  readonly deadline_ms: number;
  readonly exit_code: number;
};

type Box = {
  readonly machine: Machine;
  readonly worldId: string;
  readonly applied: Applied;
  readonly records: Map<string, Written>;
  generation: number;
  running: boolean;
  /** The sha256 of the binary that was injected at /run/threads/bin/supervise. */
  supervisor?: string;
};

export type DockerBackend = {
  readonly fetch: Fetch;
  readonly traffic: () => string;
  readonly boxes: ReadonlyMap<string, Box>;
  readonly volumes: ReadonlySet<string>;
  /** Warnings the next create answers with (a daemon that dropped a limit). */
  warnings: readonly string[];
  /** The architecture every image inspect reports. */
  architecture: string;
  /** A daemon that accepted the create and then applied no memory limit. */
  dropsMemory: boolean;
  /** Supervised runs that die before they record anything, as every `die()` path does. */
  supervisorDies: number;
};

function running(key: string, generation: string): Written {
  return {
    key,
    generation,
    state: "running",
    child_pid: 7,
    supervisor_pid: 6,
    supervisor_start: 100,
    deadline_ms: 3_600_000,
    exit_code: 0,
  };
}

/** One frame of Docker's multiplexed stream. */
function frame(stream: number, body: Uint8Array): Uint8Array {
  const out = new Uint8Array(8 + body.length);
  out[0] = stream;
  new DataView(out.buffer).setUint32(4, body.length);
  out.set(body, 8);
  return out;
}

export function dockerBackend(world: World): DockerBackend {
  const log: string[] = [];
  const boxes = new Map<string, Box>();
  const volumes = new Set<string>();
  const execs = new Map<string, (sinks: Sinks) => Promise<number>>();
  const codes = new Map<string, number>();
  let serial = 0;
  const next = () => (serial += 1);
  const generationOf = (box: Box) => `gen-${box.generation}`;

  const create = (name: string, body: unknown): Response => {
    if (boxes.has(name)) return json(409, { message: "name in use" });
    const wire = CreateBody.parse(body);
    const made = world.create(wire.Labels[OPERATION_KEY] ?? "", undefined);
    if ("missing" in made) return missing("image");
    if (made.lost) throw new TypeError("fetch failed: connection reset");
    // The sandbox id is the container name, so the World answers to it too.
    world.machines.set(name, world.machine(made.id));
    boxes.set(name, {
      machine: world.machine(made.id),
      worldId: made.id,
      applied: wire.HostConfig,
      records: new Map(),
      generation: 0,
      running: false,
    });
    for (const volume of volumesOf(name)) volumes.add(volume);
    return json(201, { Id: sha256Hex(name), Warnings: back.warnings });
  };

  /** A supervised run: admission first, then the record, then the emulated machine. */
  const supervised =
    (box: Box, hash: string, script: string) =>
    async (sinks: Sinks): Promise<number> => {
      if (back.supervisorDies > 0) {
        back.supervisorDies -= 1;
        // Every die() in mode_run is before the record: the key's earlier one is untouched.
        sinks.stderr(utf8.encode("threads: the container is not ready\n"));
        return 1;
      }
      const generation = generationOf(box);
      const live = [...box.records.values()].some(
        (r) =>
          r.generation === generation &&
          (r.state === "running" || r.state === "stuck"),
      );
      if (live) {
        sinks.stderr(utf8.encode("threads: admission refused\n"));
        return 125;
      }
      const record = running(hash, generation);
      box.records.set(hash, record);
      const code = await box.machine.start(script, sinks, hash).exit;
      // A command the probe swept is already recorded terminated; only its own supervisor
      // writes the exit.
      if (box.records.get(hash)?.state === "running")
        box.records.set(hash, { ...record, state: "exited", exit_code: code });
      // `mode_run` returns EX_OK once it has recorded: the command's code is in the record,
      // never in the exec's status. Answering `code` here would hide a host that read the
      // wrong one.
      return 0;
    };

  /** The D-2 probe: the supervisor's one sweep takes every command process with it. */
  const probe = (box: Box, hash: string): number => {
    const record = box.records.get(hash);
    if (
      record === undefined ||
      record.generation !== generationOf(box) ||
      (record.state !== "running" && record.state !== "stuck")
    )
      return 0;
    for (const key of [...box.machine.procs.keys()]) box.machine.stop(key);
    box.records.set(hash, { ...record, state: "terminated" });
    return 11;
  };

  /** What a `Cmd` runs, as the supervisor's argv contract reads it. */
  const dispatch = (
    box: Box,
    cmd: readonly string[],
  ): ((sinks: Sinks) => Promise<number>) => {
    const [head, ...rest] = cmd;
    if (head !== "/run/threads/bin/supervise")
      return async (sinks) => box.machine.start(rest[1] ?? "", sinks).exit;
    if (rest[0] === "--check")
      return async (sinks) => {
        sinks.stdout(utf8.encode(TOOLS));
        return 0;
      };
    if (rest[0] === "--terminate") return async () => probe(box, rest[1] ?? "");
    // supervise <key-hash> <deadline_ms> /bin/sh -c <script>
    return supervised(box, rest[0] ?? "", rest[4] ?? "");
  };

  const stream = (id: string): Response =>
    new Response(
      new ReadableStream<Uint8Array>({
        start: (controller) => {
          const run = execs.get(id);
          const sinks: Sinks = {
            stdout: (b) => controller.enqueue(frame(1, b)),
            stderr: (b) => controller.enqueue(frame(2, b)),
          };
          void (async () => {
            codes.set(id, (await run?.(sinks)) ?? 1);
            controller.close();
          })();
        },
      }),
    );

  const archiveGet = (box: Box, at: string): Response => {
    if (at === "/run/threads/state") {
      const files = [
        { name: "state/generation", bytes: utf8.encode(generationOf(box)) },
        ...[...box.records].map(([key, record]) => ({
          name: `state/records/${key}.json`,
          bytes: utf8.encode(JSON.stringify(record)),
        })),
      ].map((f) => ({ ...f, mode: 0o600, uid: 0, gid: 0 }));
      return new Response(tarOf(files).slice());
    }
    const bytes = box.machine.files.get(at)?.bytes;
    if (bytes === undefined) return missing(`file ${at}`);
    return new Response(
      tarOf([
        {
          name: at.slice(at.lastIndexOf("/") + 1),
          mode: 0o644,
          uid: 1000,
          gid: 1000,
          bytes,
        },
      ]).slice(),
    );
  };

  const archivePut = async (
    box: Box,
    at: string,
    req: Request,
  ): Promise<Response> => {
    for (const file of untar(new Uint8Array(await req.arrayBuffer()))) {
      if (at === "/run/threads") box.supervisor = sha256Hex(file.bytes);
      else box.machine.write(`${at}/${file.name}`, file.bytes);
    }
    return empty(200);
  };

  const listing = (url: URL): Response => {
    const filters = z
      .object({ label: z.array(z.string()) })
      .parse(JSON.parse(url.searchParams.get("filters") ?? "{}"));
    const key = (filters.label[0] ?? "").slice(`${OPERATION_KEY}=`.length);
    const found = world.find(key);
    if (found === "error") return json(500, { message: "failed" });
    const name = [...boxes].find(([, box]) => box.worldId === found)?.[0];
    return json(200, name === undefined ? [] : [{ Names: [`/${name}`] }]);
  };

  const inspected = (name: string, box: Box): Response =>
    json(200, {
      Id: sha256Hex(name),
      State: { Running: box.running },
      HostConfig: {
        NanoCpus: box.applied.NanoCpus ?? 0,
        Memory: back.dropsMemory ? 0 : (box.applied.Memory ?? 0),
        PidsLimit: box.applied.PidsLimit,
      },
    });

  const execCreate = async (box: Box, req: Request): Promise<Response> => {
    if (!box.running) return json(409, { message: "is not running" });
    const id = `exec-${next()}`;
    execs.set(id, dispatch(box, ExecBody.parse(await req.json()).Cmd));
    return json(201, { Id: id });
  };

  const removed = (name: string, box: Box): Response => {
    boxes.delete(name);
    world.kill(box.worldId);
    world.machines.delete(name);
    return empty(204);
  };

  function containerRoute(
    req: Request,
    url: URL,
    name: string,
    tail: string,
  ): Promise<Response> | Response {
    const box = boxes.get(name);
    if (box === undefined) return missing(`container ${name}`);
    const at = url.searchParams.get("path") ?? "";
    switch (`${req.method} ${tail}`) {
      case "GET /json":
        return inspected(name, box);
      case "POST /start":
        if (!box.running) box.generation += 1;
        box.running = true;
        return empty(204);
      case "POST /exec":
        return execCreate(box, req);
      case "GET /archive":
        return archiveGet(box, at);
      case "PUT /archive":
        return archivePut(box, at, req);
      case "DELETE ":
        return removed(name, box);
      default:
        return json(400, { message: `unmocked ${req.method} ${name}${tail}` });
    }
  }

  /** The endpoints with no id in the path. */
  function flat(path: string, url: URL, body: string): Response {
    if (path === "/containers/create")
      return create(url.searchParams.get("name") ?? "", JSON.parse(body));
    if (path === "/containers/json") return listing(url);
    if (path === "/images/create") return new Response(`{"status":"pulled"}\n`);
    if (path.startsWith("/images/") && path.endsWith("/json"))
      return json(200, { Architecture: back.architecture });
    return json(400, { message: `unmocked ${path}` });
  }

  const execRoute = (id: string, start: boolean): Response =>
    start ? stream(id) : json(200, { ExitCode: codes.get(id) ?? null });

  function route(
    req: Request,
    url: URL,
    body: string,
  ): Promise<Response> | Response {
    const path = url.pathname.replace("/v1.44", "");
    const [, id = "", what = ""] =
      /^\/exec\/([^/]+)\/(start|json)$/.exec(path) ?? [];
    if (id !== "") return execRoute(id, what === "start");
    const [, volume = ""] = /^\/volumes\/([^/]+)$/.exec(path) ?? [];
    if (volume !== "")
      return volumes.delete(volume) ? empty(204) : missing("volume");
    const [, name = "", tail = ""] =
      /^\/containers\/([^/]+)(\/[a-z]+)?$/.exec(path) ?? [];
    if (name !== "" && name !== "create" && name !== "json")
      return containerRoute(req, url, name, tail);
    return flat(path, url, body);
  }

  const back: DockerBackend = {
    fetch: async (input, init) => {
      const req = new Request(input, init);
      const url = new URL(req.url);
      const body = req.method === "GET" ? "" : await req.clone().text();
      log.push(`${req.method} ${req.url} ${body.slice(0, 2000)}`);
      return route(req, url, body);
    },
    traffic: () => log.join("\n"),
    boxes,
    volumes,
    warnings: [],
    architecture: "arm64",
    dropsMemory: false,
    supervisorDies: 0,
  };
  return back;
}
