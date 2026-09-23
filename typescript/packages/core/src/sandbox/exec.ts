import type { ArtifactRef } from "../log";
import { ok, type Result } from "../result";
import type { ArtifactStore } from "../store/artifacts";
import type { ExecOptions, Failure, SandboxSession } from "./protocol";

// The sandbox layer over an adapter's exec: both
// streams go to one artifact as they arrive, in arrival order, while only head and tail
// previews stay in memory. The artifact is kept only when a stream outgrew its preview.

export type ExecResult = {
  readonly exit_code: number;
  /** Head and tail preview. */
  readonly stdout: string;
  readonly stderr: string;
  readonly truncated: boolean;
  /** Present exactly when truncated: the complete stdout and stderr. */
  readonly full_output?: ArtifactRef;
};

/** Bytes of head, and of tail, each stream keeps for its preview. */
export const PREVIEW_BYTES = 4096;

const text = new TextDecoder();

/** The first and last `keep` bytes of a stream, and how many there were. */
class Preview {
  #head: Uint8Array = new Uint8Array(0);
  #tail: Uint8Array = new Uint8Array(0);
  total = 0;

  constructor(readonly keep: number) {}

  add(chunk: Uint8Array): void {
    this.total += chunk.length;
    const room = this.keep - this.#head.length;
    if (room > 0) this.#head = concat(this.#head, chunk.subarray(0, room));
    const rest = chunk.subarray(Math.max(room, 0));
    if (rest.length > 0)
      this.#tail = concat(this.#tail, rest).slice(-this.keep);
  }

  get truncated(): boolean {
    return this.total > this.#head.length + this.#tail.length;
  }

  toString(): string {
    const omitted = this.total - this.#head.length - this.#tail.length;
    const marker = omitted > 0 ? `\n[... ${omitted} bytes omitted ...]\n` : "";
    return `${text.decode(this.#head)}${marker}${text.decode(this.#tail)}`;
  }
}

function concat(a: Uint8Array, b: Uint8Array): Uint8Array {
  const out = new Uint8Array(a.length + b.length);
  out.set(a);
  out.set(b, a.length);
  return out;
}

async function pump(
  stream: AsyncIterable<Uint8Array>,
  preview: Preview,
  write: (chunk: Uint8Array) => void,
): Promise<void> {
  for await (const chunk of stream) {
    preview.add(chunk);
    write(chunk);
  }
}

/** Runs `command` in the session and spills its output at the source. */
export async function execute(
  session: SandboxSession,
  command: readonly string[],
  options: ExecOptions,
  artifacts: ArtifactStore,
  keep: number = PREVIEW_BYTES,
): Promise<
  Result<ExecResult, Failure<"timeout" | "invalid_path" | "unavailable">>
> {
  const started = await session.exec(command, options);
  if (!started.ok) return started;
  const output = started.value;
  const sink = artifacts.sink();
  const out = new Preview(keep);
  const errs = new Preview(keep);
  try {
    await Promise.all([
      pump(output.stdout, out, sink.write),
      pump(output.stderr, errs, sink.write),
    ]);
  } catch (error) {
    sink.abort();
    throw error;
  }
  const exitCode = await output.exit_code;
  const truncated = out.truncated || errs.truncated;
  const base = {
    exit_code: exitCode,
    stdout: out.toString(),
    stderr: errs.toString(),
    truncated,
  };
  if (!truncated) {
    sink.abort();
    return ok(base);
  }
  const full = sink.finish();
  return ok({
    ...base,
    full_output: { ...full, media_type: "application/octet-stream" },
  });
}
