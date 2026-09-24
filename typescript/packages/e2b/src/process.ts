import type { Sinks, Started } from "@threads/core/adapter";
import { z } from "zod";
import { E2bError, parsed } from "./wire";

// One exec's `process.Process/Start` stream, read into the kit's sinks and exit code.
//
// The messages are protobuf JSON of envd's `process.StartResponse`, from
// `spec/envd/process/process.proto` in e2b-dev/E2B at ccaf9fc0ffe6ac39c7ec786af7608ab1de19467b
// (tag e2b@2.51.0, the SDK Python's adapter decodes it with): camelCase names, `bytes` as
// base64, a field at its default (a zero exit code) left out, and at most one member of each
// `oneof`.

const Base64 = z.base64();

/** At most one member of a protobuf `oneof` is set. */
const oneof = (value: object): boolean =>
  Object.values(value).filter((v) => v !== undefined).length <= 1;

const Event = z
  .object({
    start: z.object({ pid: z.int().optional() }).optional(),
    data: z
      .object({
        stdout: Base64.optional(),
        stderr: Base64.optional(),
        pty: Base64.optional(),
      })
      .refine(oneof, "several outputs in one data event")
      .optional(),
    end: z
      .object({
        exitCode: z.int().optional(),
        exited: z.boolean().optional(),
        status: z.string().optional(),
        error: z.string().optional(),
      })
      .optional(),
    keepalive: z.object({}).optional(),
  })
  .refine(oneof, "several events in one message");

const StartResponse = z.object({ event: Event.optional() });

const lost = (when: string) =>
  new E2bError("unavailable", `envd ended the process stream ${when}`);

/** Base64 as bytes, where `Uint8Array.fromBase64` is missing (Node before 25). */
const bytes = (b64: string): Uint8Array =>
  Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));

/** The event a message carries, its output already passed to `sinks`. */
function event(
  message: string,
  sinks: Sinks,
): z.infer<typeof Event> | undefined {
  const { event } = parsed(StartResponse, message, "process event");
  const data = event?.data;
  if (data?.stdout !== undefined) sinks.stdout(bytes(data.stdout));
  if (data?.stderr !== undefined) sinks.stderr(bytes(data.stderr));
  return event;
}

/**
 * Output up to the start event, which it waits for. It steps the stream by hand: a `for await`
 * that returns would close it, and the rest of the output follows.
 */
async function started(
  events: AsyncGenerator<string>,
  sinks: Sinks,
): Promise<void> {
  for (let next = await events.next(); !next.done; next = await events.next())
    if (event(next.value, sinks)?.start !== undefined) return;
  throw lost("before it started");
}

/** Output from the start on, then the exit code (protobuf JSON leaves a zero out). */
async function exited(
  events: AsyncGenerator<string>,
  sinks: Sinks,
): Promise<number> {
  try {
    for await (const message of events) {
      const end = event(message, sinks)?.end;
      if (end !== undefined) return end.exitCode ?? 0;
    }
    throw lost("without its exit");
  } finally {
    await events.return(undefined);
  }
}

/** Resolves once envd reports the process started; its output streams on into `sinks`. */
export async function follow(
  events: AsyncGenerator<string>,
  sinks: Sinks,
): Promise<Started> {
  try {
    await started(events, sinks);
  } catch (error) {
    await events.return(undefined);
    throw error;
  }
  return { exit: exited(events, sinks) };
}
