import { existsSync } from "node:fs";
import { request } from "node:http";
import { homedir } from "node:os";
import { ConfigError, type Fetch } from "@threads/core/adapter";
import { unavailable } from "./wire";

// Where the Docker Engine listens, and the Fetch that talks to it over that unix socket.
// Discovery reads env and the filesystem only: `setup()` opens no connection (the
// Sandbox.setup contract), so a daemon that is down is found at the first real request.

/** Where a socket is looked for when DOCKER_HOST is unset, in this order. */
const CANDIDATES = ["/var/run/docker.sock", "~/.docker/run/docker.sock"];

export const UNREACHABLE: string = `Docker isn't running (looked for ${CANDIDATES.join(", ")}): start Docker, set DOCKER_HOST, or pass another sandbox (devSandbox(), e2b())`;

const expanded = (path: string): string =>
  path.startsWith("~/") ? `${homedir()}/${path.slice(2)}` : path;

/**
 * The Engine socket: DOCKER_HOST when set (unix:// only), else the first candidate that
 * exists. No connection is opened.
 */
export function resolveSocket(
  env: Readonly<Record<string, string | undefined>> = process.env,
  /** Seam: what counts as present on disk, so a test can ask for a machine with no socket. */
  here: (path: string) => boolean = existsSync,
): string {
  const host = env["DOCKER_HOST"];
  if (host !== undefined && host !== "") {
    if (!host.startsWith("unix://"))
      throw new ConfigError(
        "invalid_config",
        `docker: DOCKER_HOST must be a unix:// socket, not ${host}`,
      );
    return host.slice("unix://".length);
  }
  const found = CANDIDATES.map(expanded).find((path) => here(path));
  if (found === undefined)
    throw new ConfigError("docker_unreachable", UNREACHABLE);
  return found;
}

/** A connect failure, said in the words the operator can act on. */
function connectFailed(socket: string, error: unknown): Error {
  const code =
    error instanceof Error && "code" in error ? String(error.code) : "";
  if (code === "EACCES" || code === "EPERM")
    return unavailable(
      `no permission to use ${socket}: add your user to the docker group, or use rootless Docker`,
    );
  return unavailable(UNREACHABLE);
}

/** The response headers node gives back, as a Headers. */
function headersOf(raw: NodeJS.Dict<string | string[]>): Headers {
  const headers = new Headers();
  for (const [name, value] of Object.entries(raw))
    for (const one of Array.isArray(value) ? value : [value ?? ""])
      headers.append(name, one);
  return headers;
}

/** The request bodies this adapter sends: JSON text, or an archive as a blob. */
async function sendable(
  body: RequestInit["body"],
): Promise<Uint8Array | undefined> {
  if (body === undefined || body === null) return undefined;
  if (typeof body === "string") return new TextEncoder().encode(body);
  if (body instanceof Blob) return new Uint8Array(await body.arrayBuffer());
  throw unavailable("the Docker adapter sends only text and archive bodies");
}

/**
 * A Fetch over the Engine's unix socket, built on node:http (Bun and Node alike). The body
 * streams, so an exec's multiplexed output arrives as it is written. One attempt: nothing
 * here retries and no redirect is followed.
 */
export function socketFetch(socket: () => string): Fetch {
  return async (input, init) => {
    const url = new URL(String(input));
    const at = socket();
    const bytes = await sendable(init?.body);
    const headers: Record<string, string> = { Host: "docker" };
    for (const [name, value] of new Headers(init?.headers))
      headers[name] = value;
    if (bytes !== undefined) headers["content-length"] = String(bytes.length);
    return new Promise<Response>((resolve, reject) => {
      const req = request(
        {
          socketPath: at,
          path: url.pathname + url.search,
          method: init?.method ?? "GET",
          headers,
        },
        (res) => {
          const stream = new ReadableStream<Uint8Array>({
            start: (controller) => {
              res.on("data", (chunk: Buffer) =>
                controller.enqueue(new Uint8Array(chunk)),
              );
              res.on("end", () => controller.close());
              res.on("error", (error) => controller.error(error));
            },
            cancel: () => {
              res.destroy();
            },
          });
          const status = res.statusCode ?? 502;
          resolve(
            new Response(status === 204 || status === 304 ? null : stream, {
              status,
              headers: headersOf(res.headers),
            }),
          );
        },
      );
      req.on("error", (error) => reject(connectFailed(at, error)));
      if (bytes !== undefined) req.write(bytes);
      req.end();
    });
  };
}
