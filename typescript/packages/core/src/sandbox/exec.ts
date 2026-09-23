import { assertNever } from "../assert-never";
import type { ArtifactRef } from "../log";
import type { ToolRun } from "../loop/types";
import { err, ok, type Result } from "../result";
import type { ArtifactStore } from "../store/artifacts";
import type {
  ExecOptions,
  ExecOutput,
  Failure,
  SandboxContext,
  SandboxSession,
  Stale,
} from "./protocol";

// The sandbox layer over an adapter's exec (spec/api.json ExecResult): both
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

export type ExecFailure =
  | Failure<"timeout" | "invalid_path" | "unavailable">
  | Stale;

/**
 * An exec's outcome as a tool run: a timeout or a
 * transport failure after dispatch is uncertain (effect_unknown), never a result; a refused
 * fence sent nothing.
 */
export function toolRunOf(result: Result<ExecResult, ExecFailure>): ToolRun {
  if (result.ok)
    return {
      kind: "done",
      output: JSON.stringify(result.value),
      isError: result.value.exit_code !== 0,
    };
  const { code, message } = result.error;
  switch (code) {
    case "timeout":
      return { kind: "unknown", reason: "timeout" };
    case "unavailable":
      return { kind: "unknown", reason: "transport_error" };
    case "stale_epoch":
    case "cleanup_claim_lost":
      return { kind: "not_sent" };
    case "invalid_path":
      return { kind: "done", output: message, isError: true };
    default:
      return assertNever(code);
  }
}

/** Runs `command` in the session and spills its output at the source. */
export async function execute(
  session: SandboxSession,
  command: readonly string[],
  context: SandboxContext,
  options: ExecOptions,
  artifacts: ArtifactStore,
  keep: number = PREVIEW_BYTES,
): Promise<Result<ExecResult, ExecFailure>> {
  const started = await session.exec(command, context, options);
  if (!started.ok) return started;
  const { timeoutMs } = options;
  const collected = collect(started.value, artifacts, keep);
  if (timeoutMs === undefined) return collected;
  // After a timeout nobody awaits it; a stream that breaks later is not a crash.
  collected.catch(() => undefined);
  // Once the deadline passes the outcome is a timeout, whatever the process does next: the
  // kill is best effort and unconfirmed, so the effect stays unknown.
  const deadline = Promise.withResolvers<Result<ExecResult, ExecFailure>>();
  const timer = setTimeout(() => {
    deadline.resolve(
      err({ code: "timeout", message: `no exit within ${timeoutMs} ms` }),
    );
    void session.terminate(options.processKey, context);
  }, timeoutMs);
  try {
    return await Promise.race([collected, deadline.promise]);
  } finally {
    clearTimeout(timer);
  }
}

async function collect(
  output: ExecOutput,
  artifacts: ArtifactStore,
  keep: number,
): Promise<Result<ExecResult, ExecFailure>> {
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
