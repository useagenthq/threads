import type { Json, ModelContext } from "@threads/core/adapter";
import {
  assertNever,
  JsonObject,
  OutputPart,
  putJson,
} from "@threads/core/adapter";
import { z } from "zod";

// One finished Messages API content block → the ordered parts it records. Blocks are
// provider output, so they are parsed; thinking and hosted tool blocks are stored as the exact
// JSON the stream assembled, to go back unchanged.

const WebCitation = z.object({
  type: z.literal("web_search_result_location"),
  url: z.string(),
  title: z.string().nullish(),
  cited_text: z.string(),
});
const DocumentCitation = z.object({
  type: z.enum(["char_location", "page_location", "content_block_location"]),
  document_index: z.int().nonnegative(),
  document_title: z.string().nullish(),
  cited_text: z.string(),
});
const Citation = z.discriminatedUnion("type", [WebCitation, DocumentCitation]);

const Block = z.discriminatedUnion("type", [
  z.object({
    type: z.literal("text"),
    text: z.string(),
    citations: z.array(Citation).nullish(),
  }),
  z.object({
    type: z.literal("thinking"),
    thinking: z.string(),
    signature: z.string(),
  }),
  z.object({ type: z.literal("redacted_thinking"), data: z.string() }),
  z.object({
    type: z.literal("tool_use"),
    id: z.string(),
    name: z.string(),
    input: JsonObject,
  }),
]);
const Hosted = z.object({ type: z.string(), name: z.string().optional() });
const LOCAL = new Set(["text", "thinking", "redacted_thinking", "tool_use"]);

export type BlockContext = {
  readonly model: string;
  readonly context: ModelContext;
  /** Document artifact sha256 by document_index, as the request sent them. */
  readonly documents: readonly string[];
};

export async function blockParts(
  raw: { readonly [key: string]: Json },
  ctx: BlockContext,
): Promise<readonly OutputPart[]> {
  if (!LOCAL.has(Hosted.parse(raw).type)) return [await hosted(raw, ctx)];
  const b = Block.parse(raw);
  switch (b.type) {
    case "text":
      return [
        OutputPart.parse({ type: "text", text: b.text }),
        ...(b.citations ?? []).map((c) => citation(c, ctx.documents)),
      ];
    case "thinking":
    case "redacted_thinking":
      return [
        OutputPart.parse({
          type: "reasoning",
          provider: "anthropic",
          model: ctx.model,
          format: b.type,
          ref: await putJson(ctx.context, raw),
          ...(b.type === "thinking" && b.thinking !== ""
            ? { summary: b.thinking }
            : {}),
        }),
      ];
    case "tool_use":
      return [
        OutputPart.parse({
          type: "tool_use",
          call_id: b.id,
          name: b.name,
          input: b.input,
        }),
      ];
    default:
      return assertNever(b);
  }
}

/** A provider-executed tool use or result: kept whole, never dispatched locally. */
async function hosted(
  raw: { readonly [key: string]: Json },
  ctx: BlockContext,
): Promise<OutputPart> {
  const { type, name } = Hosted.parse(raw);
  return OutputPart.parse({
    type: "hosted_tool",
    provider: "anthropic",
    model: ctx.model,
    format: type,
    name: name ?? type,
    ref: await putJson(ctx.context, raw),
  });
}

function citation(
  c: z.infer<typeof Citation>,
  documents: readonly string[],
): OutputPart {
  const title = (t: string | null | undefined) =>
    t === null || t === undefined ? {} : { title: t };
  if (c.type === "web_search_result_location")
    return OutputPart.parse({
      type: "citation",
      source_kind: "web",
      source_id: c.url,
      ...title(c.title),
      cited_text: c.cited_text,
    });
  return OutputPart.parse({
    type: "citation",
    source_kind: "document",
    source_id: documents[c.document_index],
    ...title(c.document_title),
    cited_text: c.cited_text,
  });
}
