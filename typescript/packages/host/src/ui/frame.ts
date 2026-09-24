import type { Json } from "@threads/core/host";

// One frame of a UI stream (spec/schema/ui/README.md): a protocol chunk (an AI SDK UI message
// chunk or an AG-UI event) and, for a frame derived from a committed event, its SSE id
// <seq>:<k>. Envelope and live frames carry no id.

export type Protocol = "ai-sdk" | "ag-ui";

export type Chunk = { readonly type: string; readonly [key: string]: Json };

export type Frame = { readonly id?: string; readonly data: Chunk };

export const PROTOCOLS: readonly Protocol[] = ["ai-sdk", "ag-ui"];

export function isProtocol(value: string): value is Protocol {
  return value === "ai-sdk" || value === "ag-ui";
}

/** The chunks as frames with no SSE id. */
export function bare(chunks: readonly Chunk[]): readonly Frame[] {
  return chunks.map((data) => ({ data }));
}
