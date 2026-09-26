import type { Sinks, Started } from "@threads/core/adapter";
import { z } from "zod";
import { type Engine, failure } from "./engine";
import { parsed, unavailable } from "./wire";

// One exec through the Engine API: the attach reply is Docker's multiplexed stream, whose
// 8-byte frame headers are parsed strictly (container output is a trust boundary, so an
// unknown stream byte or an absurd length is a typed failure, never a guess). The exit code
// comes from the exec's own record, after the stream ends.

const ExecCreated = z.object({ Id: z.string().min(1) });
const ExecInspected = z.object({ ExitCode: z.int().nullish() });

/** The most bytes one frame may claim: beyond this the stream is not Docker's. */
const MAX_FRAME = 64 * 2 ** 20;
const HEADER = 8;
/** How often, and how long apart, an exit code the daemon hasn't stored yet is looked for. */
const SETTLE_LOOKS = 5;
const SETTLE_MS = 20;

export type ExecSpec = {
  /** "0" for the supervisor and its probes, "1000" for the kit's own scripts. */
  readonly user: string;
  readonly cmd: readonly string[];
};

/** Feeds Docker's multiplexed stream into the kit's sinks, one frame at a time. */
/** One frame header: `[stream, 0, 0, 0, length:u32be]`, or a typed failure. */
function headerAt(held: Uint8Array): {
  readonly stream: 1 | 2;
  readonly length: number;
} {
  const stream = held[0];
  if (stream !== 1 && stream !== 2)
    throw unavailable(
      `Docker's output stream carries an unknown frame type ${stream}`,
    );
  const length = new DataView(held.buffer, held.byteOffset + 4, 4).getUint32(0);
  if (length > MAX_FRAME)
    throw unavailable(
      `Docker's output stream claims a frame of ${length} bytes`,
    );
  return { stream, length };
}

export function demux(sinks: Sinks): {
  readonly push: (chunk: Uint8Array) => void;
  readonly end: () => void;
} {
  let held = new Uint8Array(0);
  return {
    push: (chunk) => {
      const joined = new Uint8Array(held.length + chunk.length);
      joined.set(held);
      joined.set(chunk, held.length);
      held = joined;
      while (held.length >= HEADER) {
        const { stream, length } = headerAt(held);
        if (held.length < HEADER + length) return;
        const body = held.slice(HEADER, HEADER + length);
        held = held.slice(HEADER + length);
        if (stream === 1) sinks.stdout(body);
        else sinks.stderr(body);
      }
    },
    end: () => {
      if (held.length > 0)
        throw unavailable("Docker's output stream ends inside a frame");
    },
  };
}

/** Creates the exec, starting the container first when a stopped one refused it. */
async function created(
  engine: Engine,
  name: string,
  spec: ExecSpec,
): Promise<string> {
  const body = {
    json: {
      AttachStdout: true,
      AttachStderr: true,
      AttachStdin: false,
      Tty: false,
      User: spec.user,
      Env: [],
      WorkingDir: "/workspace",
      Cmd: spec.cmd,
    },
  };
  const at = `/containers/${encodeURIComponent(name)}/exec`;
  const first = await engine.send("POST", at, body);
  if (first.status !== 409) {
    if (!first.ok) throw await failure(first, "an exec create");
    return parsed(ExecCreated, await first.text(), "an exec create").Id;
  }
  // A stopped container is started again by the next exec; attach never starts one.
  await first.text();
  const started = await engine.send(
    "POST",
    `/containers/${encodeURIComponent(name)}/start`,
  );
  if (!started.ok && started.status !== 304)
    throw await failure(started, "a container start");
  await started.text();
  return (await engine.json(ExecCreated, "an exec create", "POST", at, body))
    .Id;
}

/**
 * The exec's own exit status. The daemon can still be storing it when the output stream
 * closes, so a null is looked at again a few times before it is taken as an answer.
 */
async function exitCode(engine: Engine, id: string): Promise<number | null> {
  for (let look = 0; ; look += 1) {
    const res = await engine.send(
      "GET",
      `/exec/${encodeURIComponent(id)}/json`,
    );
    if (!res.ok) throw await failure(res, "an exec inspect");
    const code = parsed(
      ExecInspected,
      await res.text(),
      "an exec inspect",
    ).ExitCode;
    if (code !== undefined && code !== null) return code;
    if (look >= SETTLE_LOOKS) return null;
    await new Promise((done) => setTimeout(done, SETTLE_MS));
  }
}

/**
 * Starts `spec` in the container and streams its output into `sinks`. Resolves once Docker
 * accepted the attach; `exit` resolves with the exec's code once the stream has ended.
 */
export async function startExec(
  engine: Engine,
  name: string,
  spec: ExecSpec,
  sinks: Sinks,
  /** What an exec that ends with no code means; a terminate probe reads it as its own row. */
  noCode: () => number,
): Promise<Started> {
  const id = await created(engine, name, spec);
  const res = await engine.send(
    "POST",
    `/exec/${encodeURIComponent(id)}/start`,
    {
      json: { Detach: false, Tty: false },
    },
  );
  if (!res.ok) throw await failure(res, "an exec start");
  const body = res.body;
  if (body === null) throw unavailable("Docker sent no output stream");
  const frames = demux(sinks);
  const exit = (async () => {
    for await (const chunk of body) frames.push(chunk);
    frames.end();
    return (await exitCode(engine, id)) ?? noCode();
  })();
  return { exit };
}
