import { readFileSync } from "node:fs";
import { join } from "node:path";
import {
  type BaseEvent,
  defaultApplyEvents,
  HttpAgent,
  type Message,
  verifyEvents,
} from "@ag-ui/client";
import {
  readUIMessageStream,
  type UIMessage,
  type UIMessageChunk,
  UIMessageStreamError,
  uiMessageChunkSchema,
} from "ai";
import Ajv2020 from "ajv/dist/2020";
import { from, lastValueFrom, toArray } from "rxjs";
import { z } from "zod";
import { AgUiFold } from "../../src/ui/ag-ui-fold";
import type { Chunk } from "../../src/ui/frame";

// The stock clients as the `ui` runner uses them: every frame checked against the pinned
// protocol schema, the stream run through each client's own sequence checks, and the messages
// each client builds, in the case files' projection.

export const UI_SCHEMAS: string = join(
  import.meta.dir,
  "../../../../../spec/schema/ui",
);

const agUiSchema: unknown = JSON.parse(
  readFileSync(join(UI_SCHEMAS, "ag-ui-1.0.schema.json"), "utf8"),
);
const ajv = new Ajv2020({ strict: false, validateFormats: false });
const compiled = ajv.compile(
  z.record(z.string(), z.unknown()).parse(agUiSchema),
);

/** Schema errors of AI SDK chunks against ai@7.0.113's own uiMessageChunkSchema. */
export async function aiSdkProblems(
  chunks: readonly Chunk[],
): Promise<string[]> {
  const schema = uiMessageChunkSchema();
  const out: string[] = [];
  for (const c of chunks) {
    const r = await schema.validate?.(c);
    if (r !== undefined && !r.success)
      out.push(`${c.type}: ${String(r.error)}`);
  }
  return out;
}

const agUiEvent = (value: unknown): boolean => compiled(value);

export function agUiProblems(chunks: readonly Chunk[]): string[] {
  return chunks.flatMap((c) =>
    agUiEvent(c) ? [] : [`${c.type}: ${ajv.errorsText(compiled.errors)}`],
  );
}

const Chunks = z.array(z.custom<UIMessageChunk>((v) => typeof v === "object"));

/** The assistant message processUIMessageStream assembles; a sequence error throws. */
export async function aiSdkFold(
  chunks: readonly Chunk[],
): Promise<UIMessage | undefined> {
  const errors: unknown[] = [];
  let last: UIMessage | undefined;
  const stream = readUIMessageStream({
    stream: streamOf(Chunks.parse(chunks)),
    onError: (e) => errors.push(e),
  });
  for await (const m of stream) last = m;
  const broken = errors.find((e) => e instanceof UIMessageStreamError);
  if (broken !== undefined) throw broken;
  return last;
}

function streamOf<T>(items: readonly T[]): ReadableStream<T> {
  return new ReadableStream({
    start(controller) {
      for (const item of items) controller.enqueue(item);
      controller.close();
    },
  });
}

const Events = z.array(z.custom<BaseEvent>((v) => typeof v === "object"));

/** The stock AG-UI client's messages: its verifier, then its own apply. */
export async function agUiFold(chunks: readonly Chunk[]): Promise<unknown[]> {
  return [...(await agUiFoldFrom([], chunks))];
}

/** The same, for a client that already holds `initial`. */
export async function agUiFoldFrom(
  initial: readonly Message[],
  chunks: readonly Chunk[],
): Promise<readonly Message[]> {
  const events = Events.parse(chunks);
  await lastValueFrom(from(events).pipe(verifyEvents(), toArray()));
  const agent = new HttpAgent({
    url: "http://host.test",
    initialMessages: [...initial],
  });
  const input = {
    threadId: "t",
    runId: "r",
    messages: [],
    tools: [],
    context: [],
    state: {},
    forwardedProps: {},
  };
  const mutations = await lastValueFrom(
    defaultApplyEvents(input, from(events), agent, []).pipe(toArray()),
    { defaultValue: [] },
  );
  return (
    mutations.findLast((m) => m.messages !== undefined)?.messages ?? initial
  );
}

/** foldAgUi over the same stream, from an empty client. */
export function threadsFold(chunks: readonly Chunk[]): unknown[] {
  const fold = new AgUiFold();
  for (const c of chunks) fold.apply(c);
  return fold.messages;
}

const AI_KEYS = [
  "type",
  "id",
  "text",
  "state",
  "toolCallId",
  "input",
  "output",
  "errorText",
  "approval",
  "preliminary",
  "data",
] as const;

const Loose = z.record(z.string(), z.unknown());

/** The case files' projection of an AI SDK message. */
export function aiSdkProjection(m: UIMessage | undefined): unknown[] {
  if (m === undefined) return [];
  const parts = m.parts.map((p) => {
    const raw = Loose.parse(p);
    return Object.fromEntries(
      AI_KEYS.flatMap((k) => (raw[k] === undefined ? [] : [[k, raw[k]]])),
    );
  });
  return [{ id: m.id, role: m.role, parts }];
}

const AG_KEYS = ["id", "role", "content", "toolCallId", "toolCalls"] as const;

/** The case files' projection of AG-UI messages. */
export function agUiProjection(messages: readonly unknown[]): unknown[] {
  return messages.map((m) => {
    const raw = Loose.parse(m);
    const kept = AG_KEYS.flatMap((k) =>
      raw[k] === undefined ? [] : [[k, raw[k]]],
    );
    return JSON.parse(JSON.stringify(Object.fromEntries(kept)));
  });
}
