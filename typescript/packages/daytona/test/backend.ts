import type { Fetch } from "@threads/core/adapter";
import { z } from "zod";
import { shellWords } from "../../core/test/sandbox/remote/machine";
import type { World } from "../../core/test/sandbox/remote/world";
import type { OpenSocket } from "../src/logs";
import { Command } from "./command";

// Daytona's wire, mocked over a World: the control plane (<api>/sandbox, /snapshots), each
// sandbox's toolbox behind the proxy (sessions, files) and the log WebSocket, as the pinned
// clients and SDK speak them. Every request and socket URL is recorded for the canary.

export const API = "https://daytona.test/api";
const PROXY = "https://proxy.daytona.test/toolbox";
const NAMED = "threads-";

const json = (status: number, body: unknown): Response =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
const missing = (what: string) => json(404, { message: `no ${what}` });
const text = new TextDecoder();

const CreateBody = z.object({
  name: z.string(),
  snapshot: z.string().optional(),
  labels: z.record(z.string(), z.string()),
});
const ExecBody = z.object({ command: z.string(), runAsync: z.boolean() });
const Named = z.object({ name: z.string() });

/** A route: method, path pattern, and a handler of the pattern's captures. */
type Route = readonly [
  string,
  RegExp,
  (req: Request, url: URL, ...ids: string[]) => Promise<Response> | Response,
];

function dispatch(
  routes: readonly Route[],
  req: Request,
  url: URL,
  path: string,
) {
  for (const [method, pattern, handle] of routes) {
    const hit = pattern.exec(path);
    if (req.method === method && hit !== null)
      return handle(req, url, ...hit.slice(1).map(decodeURIComponent));
  }
  return json(400, { message: `unmocked ${req.method} ${path}` });
}

export type DaytonaBackend = {
  readonly fetch: Fetch;
  readonly open: OpenSocket;
  readonly traffic: () => string;
  /** Sandbox states, by id. */
  readonly states: Map<string, string>;
};

export function daytonaBackend(world: World): DaytonaBackend {
  const log: string[] = [];
  const states = new Map<string, string>();
  const commands = new Map<string, Command>();
  let serial = 0;

  const dto = (id: string) => ({
    id,
    state: states.get(id) ?? "started",
    toolboxProxyUrl: PROXY,
  });
  const running = (id: string) =>
    world.machines.has(id) && states.get(id) !== "stopped";

  /** A sandbox by id, or by the name its operation key gave it (a lookup). */
  const get = (_: Request, __: URL, ref: string) => {
    if (!ref.startsWith(NAMED))
      return world.machines.has(ref) ? json(200, dto(ref)) : missing("sandbox");
    const found = world.find(ref.slice(NAMED.length));
    if (found === "error") return json(500, { message: "lookup failed" });
    return found === undefined ? missing("sandbox") : json(200, dto(found));
  };

  const create = async (req: Request) => {
    const body = CreateBody.parse(await req.json());
    const made = world.create(
      body.labels["threads_operation_key"] ?? "",
      body.snapshot,
    );
    if ("missing" in made) return missing("snapshot");
    if (made.lost) throw new TypeError("fetch failed: connection reset");
    return json(200, dto(made.id));
  };

  const state = (to: string) => (_: Request, __: URL, id: string) => {
    if (!world.machines.has(id)) return missing("sandbox");
    states.set(id, to);
    return json(200, dto(id));
  };

  const snapshot = async (req: Request, _: URL, id: string) => {
    // Daytona takes a cold snapshot only of a stopped sandbox.
    if (states.get(id) !== "stopped")
      return json(400, { message: "the sandbox must be stopped" });
    world.snapshot(id, Named.parse(await req.json()).name);
    return json(200, dto(id));
  };

  const control: readonly Route[] = [
    ["POST", /^\/sandbox$/, create],
    ["GET", /^\/sandbox\/([^/]+)$/, get],
    ["POST", /^\/sandbox\/([^/]+)\/stop$/, state("stopped")],
    ["POST", /^\/sandbox\/([^/]+)\/start$/, state("started")],
    ["POST", /^\/sandbox\/([^/]+)\/snapshot$/, snapshot],
    [
      "DELETE",
      /^\/sandbox\/([^/]+)$/,
      (_, __, id) =>
        world.kill(id)
          ? json(200, { ...dto(id), state: "destroying" })
          : missing("sandbox"),
    ],
    [
      "GET",
      /^\/snapshots\/([^/]+)$/,
      (_, __, ref) =>
        world.snapshots.has(ref)
          ? json(200, { id: ref, name: ref, state: "active" })
          : missing("snapshot"),
    ],
    [
      "DELETE",
      /^\/snapshots\/([^/]+)$/,
      (_, __, ref) =>
        world.deleteSnapshot(ref) ? json(200, {}) : missing("snapshot"),
    ],
  ];

  const exec = async (req: Request, _: URL, box: string, session: string) => {
    const { command } = ExecBody.parse(await req.json());
    serial += 1;
    const cmdId = `cmd-${serial}`;
    // sh -c '<framed wrapper>', whose `sh -c '<script>' > …` line runs the kit's script with
    // each stream hex-encoded by od, as the wrapper does in a real sandbox.
    const wrapper = shellWords(command)[2] ?? "";
    const inner = wrapper.slice(wrapper.indexOf("\nsh -c ") + 1);
    const script = shellWords(inner)[2] ?? "";
    const od = (bytes: Uint8Array) =>
      new TextEncoder().encode(
        `${[...bytes].map((b) => ` ${b.toString(16).padStart(2, "0")}`).join("")}\n`,
      );
    commands.set(
      `${box}/${session}/${cmdId}`,
      new Command((sinks) =>
        world.machine(box).start(
          script,
          {
            stdout: (b) => sinks.stdout(od(b)),
            stderr: (b) => sinks.stderr(od(b)),
          },
          session,
        ),
      ),
    );
    return json(200, { cmdId });
  };

  const status = (
    _: Request,
    __: URL,
    box: string,
    session: string,
    cmd: string,
  ) => {
    const exit = commands.get(`${box}/${session}/${cmd}`)?.exit;
    return json(200, {
      id: cmd,
      command: "",
      ...(exit === undefined ? {} : { exitCode: exit }),
    });
  };

  const upload = async (req: Request, url: URL, box: string) => {
    const part = (await req.formData()).get("file");
    if (!(part instanceof Blob)) return json(400, { message: "no file" });
    world
      .machine(box)
      .write(
        url.searchParams.get("path") ?? "",
        new Uint8Array(await part.arrayBuffer()),
      );
    return json(200, {});
  };

  const download = (_: Request, url: URL, box: string) => {
    const bytes = world
      .machine(box)
      .files.get(url.searchParams.get("path") ?? "")?.bytes;
    return bytes === undefined ? missing("file") : new Response(bytes.slice());
  };

  const toolbox: readonly Route[] = [
    ["POST", /^\/([^/]+)\/process\/session$/, () => json(201, {})],
    ["POST", /^\/([^/]+)\/process\/session\/([^/]+)\/exec$/, exec],
    ["GET", /^\/([^/]+)\/process\/session\/([^/]+)\/command\/([^/]+)$/, status],
    [
      "DELETE",
      /^\/([^/]+)\/process\/session\/([^/]+)$/,
      (_, __, box, session) => {
        world.machine(box).stop(session);
        return new Response(null, { status: 204 });
      },
    ],
    ["POST", /^\/([^/]+)\/files\/upload-v2$/, upload],
    ["GET", /^\/([^/]+)\/files\/download$/, download],
  ];

  const fetch: Fetch = async (input, init) => {
    const req =
      input instanceof Request
        ? new Request(input, init)
        : new Request(String(input), init);
    const body =
      req.method === "GET" ? "" : text.decode(await req.clone().arrayBuffer());
    log.push(`${req.method} ${req.url} ${body}`);
    const url = new URL(req.url);
    if (req.url.startsWith(API))
      return dispatch(control, req, url, url.pathname.slice("/api".length));
    if (!req.url.startsWith(PROXY))
      throw new TypeError(`no route to ${req.url}`);
    const path = url.pathname.slice("/toolbox".length);
    const box = path.split("/")[1] ?? "";
    return running(box)
      ? dispatch(toolbox, req, url, path)
      : json(502, { message: "the sandbox isn't running" });
  };

  const open: OpenSocket = (url) => {
    log.push(`WS ${url}`);
    const at =
      /toolbox\/([^/]+)\/process\/session\/([^/]+)\/command\/([^/]+)\/logs/.exec(
        url,
      );
    const command = commands.get(`${at?.[1]}/${at?.[2]}/${at?.[3]}`);
    if (command === undefined) throw new Error(`no command at ${url}`);
    return command.socket();
  };

  return { fetch, open, traffic: () => log.join("\n"), states };
}
