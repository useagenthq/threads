import type { Json, ModelContext } from "@threads/core/adapter";
import { assertNever, JsonObject, OutputPart } from "@threads/core/adapter";
import { z } from "zod";

// One finished Responses API output item → the ordered parts it records. Items are
// provider output, so they are parsed; reasoning and hosted tool items are stored as the exact
// JSON received, to go back unchanged.

const Annotation = z.discriminatedUnion("type", [
  z.object({
    type: z.literal("url_citation"),
    url: z.string(),
    title: z.string().nullish(),
  }),
  z.object({
    type: z.enum(["file_citation", "container_file_citation", "file_path"]),
    file_id: z.string(),
    filename: z.string().nullish(),
  }),
]);
const Content = z.discriminatedUnion("type", [
  z.object({
    type: z.literal("output_text"),
    text: z.string(),
    annotations: z.array(Annotation).nullish(),
  }),
  z.object({ type: z.literal("refusal"), refusal: z.string() }),
]);
const Item = z.discriminatedUnion("type", [
  z.object({ type: z.literal("message"), content: z.array(Content) }),
  z.object({
    type: z.literal("function_call"),
    call_id: z.string(),
    name: z.string(),
    arguments: z.string(),
  }),
  z.object({
    type: z.literal("reasoning"),
    summary: z.array(z.object({ text: z.string() })).nullish(),
  }),
]);
const Tagged = z.object({ type: z.string() });
const LOCAL = new Set(["message", "function_call", "reasoning"]);

const encoder = new TextEncoder();

export type ItemContext = {
  readonly model: string;
  readonly context: ModelContext;
};

/** The parts, and whether the item was a refusal. */
export async function itemParts(
  raw: { readonly [key: string]: Json },
  ctx: ItemContext,
): Promise<{
  readonly parts: readonly OutputPart[];
  readonly refused: boolean;
}> {
  const { type } = Tagged.parse(raw);
  if (!LOCAL.has(type))
    return {
      parts: [
        OutputPart.parse({
          type: "hosted_tool",
          provider: "openai",
          model: ctx.model,
          format: type,
          name: type,
          ref: await store(raw, ctx.context),
        }),
      ],
      refused: false,
    };
  const item = Item.parse(raw);
  switch (item.type) {
    case "message":
      return {
        parts: item.content.flatMap(content),
        refused: item.content.some((c) => c.type === "refusal"),
      };
    case "function_call":
      return {
        parts: [
          OutputPart.parse({
            type: "tool_use",
            call_id: item.call_id,
            name: item.name,
            input: JsonObject.parse(JSON.parse(item.arguments)),
          }),
        ],
        refused: false,
      };
    case "reasoning": {
      const summary = (item.summary ?? []).map((s) => s.text).join("\n");
      return {
        parts: [
          OutputPart.parse({
            type: "reasoning",
            provider: "openai",
            model: ctx.model,
            format: "openai_reasoning",
            ref: await store(raw, ctx.context),
            ...(summary === "" ? {} : { summary }),
          }),
        ],
        refused: false,
      };
    }
    default:
      return assertNever(item);
  }
}

/** Refusal is text plus stop_reason refusal. */
function content(c: z.infer<typeof Content>): OutputPart[] {
  if (c.type === "refusal")
    return [OutputPart.parse({ type: "text", text: c.refusal })];
  return [
    OutputPart.parse({ type: "text", text: c.text }),
    ...(c.annotations ?? []).map(citation),
  ];
}

function citation(a: z.infer<typeof Annotation>): OutputPart {
  if (a.type === "url_citation")
    return OutputPart.parse({
      type: "citation",
      source_kind: "web",
      source_id: a.url,
      ...(a.title === null || a.title === undefined ? {} : { title: a.title }),
    });
  return OutputPart.parse({
    type: "citation",
    source_kind: "document",
    source_id: a.file_id,
    ...(a.filename === null || a.filename === undefined
      ? {}
      : { title: a.filename }),
  });
}

function store(
  raw: { readonly [key: string]: Json },
  context: ModelContext,
): ReturnType<ModelContext["put"]> {
  return context.put(encoder.encode(JSON.stringify(raw)), "application/json");
}
