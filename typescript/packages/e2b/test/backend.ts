import type { Fetch } from "@threads/core/adapter";
import { z } from "zod";
import type { World } from "../../core/test/sandbox/remote/world";
import { OPERATION_KEY, PROCESS_KEY } from "../src/driver";

// E2B's wire, mocked over a World: the control plane (api.<domain>) and each sandbox's envd
// (49983-<id>.<domain>: Connect RPC for processes, HTTP for files), as the pinned SDK speaks
// them. Every request is recorded for the credential canary.

const json = (status: number, body: unknown): Response =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
const empty = (status: number): Response => new Response(null, { status });
const missing = (what: string): Response =>
  json(404, { code: 404, message: `no ${what}` });
const utf8 = new TextEncoder();
const text = new TextDecoder();
const base64 = (bytes: Uint8Array): string =>
  Buffer.from(bytes).toString("base64");

const CreateBody = z.object({
  templateID: z.string(),
  metadata: z.record(z.string(), z.string()).optional(),
});
const SnapshotBody = z.object({ name: z.string() });
const StartBody = z.object({
  process: z.object({
    args: z.array(z.string()),
    envs: z.record(z.string(), z.string()).optional(),
  }),
});
const SignalBody = z.object({ process: z.object({ pid: z.int() }) });

/** One Connect envelope: flags, big-endian length, JSON. */
function envelope(flags: number, message: unknown): Uint8Array {
  const body = utf8.encode(JSON.stringify(message));
  const out = new Uint8Array(5 + body.length);
  out[0] = flags;
  new DataView(out.buffer).setUint32(1, body.length);
  out.set(body, 5);
  return out;
}

function unenvelope(bytes: Uint8Array): unknown {
  const length = new DataView(bytes.buffer, bytes.byteOffset).getUint32(1);
  return JSON.parse(text.decode(bytes.subarray(5, 5 + length)));
}

function sandboxJson(id: string) {
  return {
    sandboxID: id,
    templateID: "base",
    clientID: "client",
    envdVersion: "0.6.0",
    envdAccessToken: "envd-token",
    domain: null,
    startedAt: new Date(0).toISOString(),
    endAt: new Date(0).toISOString(),
    cpuCount: 1,
    memoryMB: 512,
    diskSizeMB: 1024,
    state: "running",
    metadata: {},
  };
}

export type E2bBackend = {
  readonly fetch: Fetch;
  /** Every request's method, URL and body, as text. */
  readonly traffic: () => string;
};

export function e2bBackend(world: World, domain: string): E2bBackend {
  const log: string[] = [];
  const pids = new Map<
    number,
    { readonly box: string; readonly key: string }
  >();
  let pid = 100;

  const create = (body: z.infer<typeof CreateBody>): Response => {
    const snapshot = body.templateID === "base" ? undefined : body.templateID;
    const made = world.create(body.metadata?.[OPERATION_KEY] ?? "", snapshot);
    if ("missing" in made) return missing("template");
    if (made.lost) throw new TypeError("fetch failed: connection reset");
    return json(201, sandboxJson(made.id));
  };

  const list = (url: URL): Response => {
    const query = new URLSearchParams(url.searchParams.get("metadata") ?? "");
    const found = world.find(
      decodeURIComponent(query.get(OPERATION_KEY) ?? ""),
    );
    if (found === "error") return json(500, { code: 500, message: "failed" });
    return json(200, found === undefined ? [] : [sandboxJson(found)]);
  };

  const live = (id: string): Response =>
    world.machines.has(id) ? json(200, sandboxJson(id)) : missing("sandbox");

  /** Routes with an id in the path: `<METHOD> <pattern>` → handler. */
  const byId: readonly [
    string,
    RegExp,
    (id: string, req: Request) => Promise<Response> | Response,
  ][] = [
    ["GET", /^\/sandboxes\/([^/]+)$/, live],
    ["POST", /^\/v2\/sandboxes\/([^/]+)\/connect$/, live],
    [
      "DELETE",
      /^\/sandboxes\/([^/]+)$/,
      (id) => (world.kill(id) ? empty(204) : missing("sandbox")),
    ],
    [
      "POST",
      /^\/sandboxes\/([^/]+)\/snapshots$/,
      async (id, req) => {
        const { name } = SnapshotBody.parse(await req.json());
        world.snapshot(id, name);
        return json(201, { snapshotID: name, names: [name] });
      },
    ],
    [
      "DELETE",
      /^\/templates\/([^/]+)$/,
      (id) =>
        world.deleteSnapshot(decodeURIComponent(id))
          ? empty(204)
          : missing("snapshot"),
    ],
  ];

  const control = async (req: Request, url: URL): Promise<Response> => {
    if (url.pathname === "/v2/sandboxes")
      return req.method === "POST"
        ? create(CreateBody.parse(await req.json()))
        : list(url);
    for (const [method, pattern, handle] of byId) {
      const id = pattern.exec(url.pathname)?.[1];
      if (req.method === method && id !== undefined) return handle(id, req);
    }
    return json(400, { message: `unmocked ${req.method} ${url.pathname}` });
  };

  const start = (box: string, request: z.infer<typeof StartBody>): Response => {
    const key = request.process.envs?.[PROCESS_KEY];
    pid += 1;
    const me = pid;
    if (key !== undefined) pids.set(me, { box, key });
    const stream = new ReadableStream<Uint8Array>({
      start: (controller) => {
        const event = (e: unknown) =>
          controller.enqueue(envelope(0, { event: e }));
        event({ start: { pid: me } });
        const started = world.machine(box).start(
          request.process.args[2] ?? "",
          {
            stdout: (b) => event({ data: { stdout: base64(b) } }),
            stderr: (b) => event({ data: { stderr: base64(b) } }),
          },
          key,
        );
        const finish = async () => {
          const code = await started.exit;
          event({
            end: { exitCode: code, exited: true, status: `exit ${code}` },
          });
          controller.enqueue(envelope(2, {}));
          controller.close();
        };
        void finish();
      },
    });
    return new Response(stream, {
      headers: { "content-type": "application/connect+json" },
    });
  };

  const envd = async (
    req: Request,
    url: URL,
    box: string,
  ): Promise<Response> => {
    if (!world.machines.has(box)) return json(502, { message: "sandbox gone" });
    const machine = world.machine(box);
    const file = url.searchParams.get("path") ?? "";
    switch (`${req.method} ${url.pathname}`) {
      case "GET /health":
        return empty(204);
      case "POST /process.Process/Start":
        return start(
          box,
          StartBody.parse(unenvelope(new Uint8Array(await req.arrayBuffer()))),
        );
      case "POST /process.Process/List":
        return json(200, {
          processes: [...pids]
            .filter(([, p]) => p.box === box && machine.procs.has(p.key))
            .map(([n, p]) => ({
              config: {
                cmd: "/bin/bash",
                args: [],
                envs: { [PROCESS_KEY]: p.key },
              },
              pid: n,
            })),
        });
      case "POST /process.Process/SendSignal": {
        const { process } = SignalBody.parse(await req.json());
        machine.stop(pids.get(process.pid)?.key ?? "");
        return json(200, {});
      }
      case "POST /files":
        machine.write(file, new Uint8Array(await req.arrayBuffer()));
        return json(200, [{ name: file, type: "file", path: file }]);
      case "GET /files": {
        const bytes = machine.files.get(file)?.bytes;
        return bytes === undefined
          ? missing("file")
          : new Response(bytes.slice(), {
              headers: { "content-type": "application/octet-stream" },
            });
      }
      default:
        return json(400, { message: `unmocked envd ${url.pathname}` });
    }
  };

  const fetch: Fetch = async (input, init) => {
    const req = new Request(input, init);
    const body =
      req.method === "GET" ? "" : text.decode(await req.clone().arrayBuffer());
    log.push(`${req.method} ${req.url} ${body}`);
    const url = new URL(req.url);
    if (url.host === `api.${domain}`) return control(req, url);
    const box = /^49983-(.+)$/.exec(url.host.slice(0, -domain.length - 1))?.[1];
    if (box !== undefined && url.host.endsWith(`.${domain}`))
      return envd(req, url, box);
    throw new TypeError(`no route to ${url.host}`);
  };

  return { fetch, traffic: () => log.join("\n") };
}
