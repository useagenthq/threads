import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import {
  type Fetch,
  type Json,
  JsonObject,
  type ModelChunk,
  type ModelContext,
  type ModelInfo,
  memoryContext,
} from "@threads/core/adapter";

export { type CredentialCase, credentialCases } from "./credentials";
export { type DryPinCase, dryPinCases } from "./dry-pin";

// Shared by the model adapter tests: Render v1 bodies, conformance render cases and a recording
// fetch. Nothing here touches the network.

const CASES = new URL("../../../../spec/conformance/cases/", import.meta.url)
  .pathname;
const encoder = new TextEncoder();

/** Render v1 bytes from line values (line 0 first). */
export function renderBody(lines: readonly Json[]): Uint8Array {
  return encoder.encode(lines.map((l) => `${JSON.stringify(l)}\n`).join(""));
}

/**
 * A conformance case's recorded request with line 0's adapter, model and params swapped for
 * this adapter's (system and tools kept), and its artifacts in a memory context. The history
 * lines are the case's exact bytes.
 */
export async function renderCase(
  name: string,
  settings: Pick<ModelInfo, "adapter" | "model" | "params">,
): Promise<{ readonly body: Uint8Array; readonly context: ModelContext }> {
  const dir = join(CASES, name);
  const context = memoryContext();
  for (const file of readdirSync(join(dir, "artifacts")))
    await context.put(
      readFileSync(join(dir, "artifacts", file)),
      "application/octet-stream",
    );
  const [first = "", ...rest] = readFileSync(
    join(dir, "request.bytes"),
    "utf8",
  ).split("\n");
  const { adapter, model, params } = settings;
  const head = {
    ...JsonObject.parse(JSON.parse(first)),
    adapter,
    model,
    params,
  };
  return {
    body: encoder.encode([JSON.stringify(head), ...rest].join("\n")),
    context,
  };
}

/** A server-sent event stream response. */
export function sse(
  events: readonly { readonly event?: string; readonly data: Json }[],
  headers: Record<string, string> = {},
): Response {
  const text = events
    .map(
      (e) =>
        `${e.event === undefined ? "" : `event: ${e.event}\n`}data: ${JSON.stringify(e.data)}\n\n`,
    )
    .join("");
  return new Response(text, {
    status: 200,
    headers: { "content-type": "text/event-stream", ...headers },
  });
}

/** An error response with a JSON body. */
export function failure(
  status: number,
  body: Json,
  headers: Record<string, string> = {},
): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", ...headers },
  });
}

/** A fetch that answers from a queue and records each request's URL and JSON body. */
export function recordingFetch(responses: readonly Response[]): {
  readonly fetch: Fetch;
  readonly calls: { url: string; body: unknown }[];
} {
  const queue = [...responses];
  const calls: { url: string; body: unknown }[] = [];
  const fetch: Fetch = async (input, init) => {
    const body = typeof init?.body === "string" ? JSON.parse(init.body) : null;
    calls.push({ url: String(input), body });
    const next = queue.shift();
    if (next === undefined) throw new Error("recordingFetch: no response left");
    return next;
  };
  return { fetch, calls };
}

/**
 * Where a send's deltas break the ModelChunk rule, or [] when none does: each delta names the
 * index its text has in the committed parts, that part is text, and the part's deltas joined
 * are a prefix of its text (spec/schema/ui/README.md, "Live text"). A stream that broke before
 * its parts landed is checked only for what landed.
 */
export function deltaProblems(chunks: readonly ModelChunk[]): string[] {
  const parts = chunks.flatMap((c) => (c.kind === "part" ? [c.part] : []));
  const sent = new Map<number, string>();
  for (const c of chunks)
    if (c.kind === "delta") sent.set(c.part, (sent.get(c.part) ?? "") + c.text);
  return [...sent].flatMap(([index, text]) => {
    const part = parts[index];
    if (part === undefined) return [];
    if (part.type !== "text")
      return [`delta part ${index} is a ${part.type} part`];
    return part.text.startsWith(text)
      ? []
      : [`delta part ${index} text ${JSON.stringify(text)} is not a prefix`];
  });
}

/** Every chunk of one send; a thrown error comes back as `{ thrown }`. */
export async function drain(
  stream: AsyncIterable<ModelChunk>,
): Promise<{ readonly chunks: ModelChunk[]; readonly thrown?: unknown }> {
  const chunks: ModelChunk[] = [];
  try {
    for await (const chunk of stream) chunks.push(chunk);
  } catch (thrown) {
    return { chunks, thrown };
  }
  return { chunks };
}
