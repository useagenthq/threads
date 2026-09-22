import { z } from "zod";
import { ArtifactRef, AudioRef, DocumentRef, ImageRef, Span } from "./common";
import { CallId } from "./ids";
import { JsonObject, Name, NonEmpty, PosInt } from "./primitives";
import type { EnumOf, Lit, Opt, Strict } from "./zod-types";

export const TextPart: Strict<{ type: Lit<"text">; text: z.ZodString }> = z
  .strictObject({ type: z.literal("text"), text: z.string() })
  .meta({ id: "TextPart" });

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
  .meta({
    id: "ToolUsePart",
    description:
      "A locally dispatchable call proposed by the model. Only model_response and model_response_recovered may carry it; a tool_call must match one.",
  });

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
  .meta({
    id: "ImagePart",
    description:
      "An image the model sees. Bytes are always an artifact; width and height are what the adapter needs for scaling (computer use coordinates).",
  });

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
    transcript: z
      .string()
      .describe(
        "Host-produced transcript, shown only to adapters without audio input when the agent opts in.",
      )
      .optional(),
  })
  .meta({
    id: "AudioPart",
    description:
      "Recorded (non-realtime) audio, e.g. a channel voice note. Only adapters that declare audio input accept it; others fail pre-dispatch with content_unsupported. Realtime sessions are out of scope.",
  });

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
    summary: z
      .string()
      .describe("Provider-supplied readable summary, for display only.")
      .optional(),
  })
  .meta({
    id: "ReasoningPart",
    description:
      "Opaque provider continuation material (Anthropic thinking or redacted_thinking with its signature, OpenAI reasoning items with encrypted content). ref holds the exact provider bytes/JSON. Never rendered as text and never required in a UI. Render v1 emits the part as recorded; an adapter that cannot send it back fails with continuation_unsupported, never drops it silently. Only settings_changed{reasoning_carryover: omit_prior} omits earlier parts.",
  });

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
  .meta({
    id: "HostedToolPart",
    description:
      "A provider-executed tool use and its result, inside one model attempt. The execution owner is the provider: it is never turned into a tool_call or dispatched locally. ref holds the exact provider blocks; readable citations follow as citation parts.",
  });

const SOURCE_KINDS = ["web", "knowledge", "document", "tool_result"] as const;
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
  .meta({
    id: "CitationPart",
    description:
      "Annotates the text part immediately before it. source_id is a URL (web), doc_id (knowledge, with version), document artifact sha256 (document) or call_id (tool_result). ref and span name the exact cited bytes.",
  });

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
  .meta({
    id: "ContentPart",
    description:
      "Every content part. Each position allows a subset: InputPart (user_input, steer), OutputPart (model responses), ResultPart (tool results).",
  });
export type ContentPart = z.infer<typeof ContentPart>;

export const InputPart: z.ZodDiscriminatedUnion<
  [typeof TextPart, typeof ImagePart, typeof DocumentPart, typeof AudioPart],
  "type"
> = z
  .discriminatedUnion("type", [TextPart, ImagePart, DocumentPart, AudioPart])
  .meta({
    id: "InputPart",
    description:
      "What a user or channel may send. No tool_use: user content never enters dispatch.",
  });
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

export const ResultPart: z.ZodDiscriminatedUnion<
  [typeof TextPart, typeof ImagePart, typeof DocumentPart, typeof CitationPart],
  "type"
> = z
  .discriminatedUnion("type", [TextPart, ImagePart, DocumentPart, CitationPart])
  .meta({
    id: "ResultPart",
    description: "Ordered, mixed text and media of a tool result. No tool_use.",
  });
export type ResultPart = z.infer<typeof ResultPart>;
