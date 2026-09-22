import { z } from "zod";
import { ArtifactRef, AudioRef, DocumentRef, ImageRef, Span } from "./common";
import { CallId } from "./ids";
import { JsonObject, Name, NonEmpty, PosInt } from "./primitives";
import type { EnumOf, Lit, Opt, Strict } from "./zod-types";

// . Each position allows a subset of parts: InputPart, OutputPart, ResultPart.

export const TextPart: Strict<{ type: Lit<"text">; text: z.ZodString }> = z
  .strictObject({ type: z.literal("text"), text: z.string() })
  .meta({ id: "TextPart" });

/** A locally dispatchable call. Only model responses may carry it. */
export const ToolUsePart: Strict<{
  type: Lit<"tool_use">;
  call_id: typeof CallId;
  name: typeof Name;
  input: typeof JsonObject;
}> = z
  .strictObject({
    type: z.literal("tool_use"),
    call_id: CallId,
    name: Name,
    input: JsonObject,
  })
  .meta({ id: "ToolUsePart" });

export const ImagePart: Strict<{
  type: Lit<"image_ref">;
  ref: typeof ImageRef;
  width: typeof PosInt;
  height: typeof PosInt;
  alt: Opt<z.ZodString>;
}> = z
  .strictObject({
    type: z.literal("image_ref"),
    ref: ImageRef,
    width: PosInt,
    height: PosInt,
    alt: z.string().optional(),
  })
  .meta({ id: "ImagePart" });

export const DocumentPart: Strict<{
  type: Lit<"document_ref">;
  ref: typeof DocumentRef;
  title: Opt<z.ZodString>;
  pages: Opt<typeof PosInt>;
}> = z
  .strictObject({
    type: z.literal("document_ref"),
    ref: DocumentRef,
    title: z.string().optional(),
    pages: PosInt.optional(),
  })
  .meta({ id: "DocumentPart" });

export const AudioPart: Strict<{
  type: Lit<"audio_ref">;
  ref: typeof AudioRef;
  duration_ms: typeof PosInt;
  transcript: Opt<z.ZodString>;
}> = z
  .strictObject({
    type: z.literal("audio_ref"),
    ref: AudioRef,
    duration_ms: PosInt,
    transcript: z.string().optional(),
  })
  .meta({ id: "AudioPart" });

/** Opaque provider continuation material. Never rendered as text. */
export const ReasoningPart: Strict<{
  type: Lit<"reasoning">;
  provider: typeof Name;
  model: typeof NonEmpty;
  format: typeof Name;
  ref: typeof ArtifactRef;
  summary: Opt<z.ZodString>;
}> = z
  .strictObject({
    type: z.literal("reasoning"),
    provider: Name,
    model: NonEmpty,
    format: Name,
    ref: ArtifactRef,
    summary: z.string().optional(),
  })
  .meta({ id: "ReasoningPart" });

/** A provider-executed tool use and its result. Never dispatched locally. */
export const HostedToolPart: Strict<{
  type: Lit<"hosted_tool">;
  provider: typeof Name;
  model: typeof NonEmpty;
  format: typeof Name;
  name: typeof Name;
  ref: typeof ArtifactRef;
}> = z
  .strictObject({
    type: z.literal("hosted_tool"),
    provider: Name,
    model: NonEmpty,
    format: Name,
    name: Name,
    ref: ArtifactRef,
  })
  .meta({ id: "HostedToolPart" });

const SOURCE_KINDS = ["web", "knowledge", "document", "tool_result"] as const;
/** Annotates the text part immediately before it. */
export const CitationPart: Strict<{
  type: Lit<"citation">;
  source_kind: EnumOf<typeof SOURCE_KINDS>;
  source_id: typeof NonEmpty;
  version: Opt<z.ZodString>;
  ref: Opt<typeof ArtifactRef>;
  span: Opt<typeof Span>;
  title: Opt<z.ZodString>;
  cited_text: Opt<z.ZodString>;
}> = z
  .strictObject({
    type: z.literal("citation"),
    source_kind: z.enum(SOURCE_KINDS),
    source_id: NonEmpty,
    version: z.string().optional(),
    ref: ArtifactRef.optional(),
    span: Span.optional(),
    title: z.string().optional(),
    cited_text: z.string().optional(),
  })
  .meta({ id: "CitationPart" });

export const ContentPart: z.ZodDiscriminatedUnion<
  [
    typeof TextPart,
    typeof ToolUsePart,
    typeof ImagePart,
    typeof DocumentPart,
    typeof AudioPart,
    typeof ReasoningPart,
    typeof HostedToolPart,
    typeof CitationPart,
  ],
  "type"
> = z
  .discriminatedUnion("type", [
    TextPart,
    ToolUsePart,
    ImagePart,
    DocumentPart,
    AudioPart,
    ReasoningPart,
    HostedToolPart,
    CitationPart,
  ])
  .meta({ id: "ContentPart" });
export type ContentPart = z.infer<typeof ContentPart>;

/** What a user or channel may send. No tool_use: user content never enters dispatch. */
export const InputPart: z.ZodDiscriminatedUnion<
  [typeof TextPart, typeof ImagePart, typeof DocumentPart, typeof AudioPart],
  "type"
> = z
  .discriminatedUnion("type", [TextPart, ImagePart, DocumentPart, AudioPart])
  .meta({ id: "InputPart" });
export type InputPart = z.infer<typeof InputPart>;

export const OutputPart: z.ZodDiscriminatedUnion<
  [
    typeof TextPart,
    typeof ToolUsePart,
    typeof ReasoningPart,
    typeof HostedToolPart,
    typeof CitationPart,
    typeof ImagePart,
  ],
  "type"
> = z
  .discriminatedUnion("type", [
    TextPart,
    ToolUsePart,
    ReasoningPart,
    HostedToolPart,
    CitationPart,
    ImagePart,
  ])
  .meta({ id: "OutputPart" });
export type OutputPart = z.infer<typeof OutputPart>;

/** Ordered, mixed text and media of a tool result. No tool_use. */
export const ResultPart: z.ZodDiscriminatedUnion<
  [typeof TextPart, typeof ImagePart, typeof DocumentPart, typeof CitationPart],
  "type"
> = z
  .discriminatedUnion("type", [TextPart, ImagePart, DocumentPart, CitationPart])
  .meta({ id: "ResultPart" });
export type ResultPart = z.infer<typeof ResultPart>;
