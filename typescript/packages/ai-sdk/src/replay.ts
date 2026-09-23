import { JsonObject, JsonValue } from "@threads/core/adapter";
import { z } from "zod";

// The exact AI SDK prompt parts a reasoning or hosted tool part stores (its artifact), so a
// later request sends them back unchanged, provider metadata (signatures) included. Stored
// bytes cross the storage boundary, so they are parsed on the way back.

export const ProviderOptions: z.ZodRecord<z.ZodString, typeof JsonObject> =
  z.record(z.string(), JsonObject);
const Options: z.ZodExactOptional<typeof ProviderOptions> =
  ProviderOptions.exactOptional();

export const ReplayPart: z.ZodDiscriminatedUnion<
  [
    z.ZodObject<{
      type: z.ZodLiteral<"reasoning">;
      text: z.ZodString;
      providerOptions: typeof Options;
    }>,
    z.ZodObject<{
      type: z.ZodLiteral<"tool-call">;
      toolCallId: z.ZodString;
      toolName: z.ZodString;
      input: typeof JsonObject;
      providerExecuted: z.ZodLiteral<true>;
      providerOptions: typeof Options;
    }>,
    z.ZodObject<{
      type: z.ZodLiteral<"tool-result">;
      toolCallId: z.ZodString;
      toolName: z.ZodString;
      output: z.ZodObject<{
        type: z.ZodLiteral<"json">;
        value: typeof JsonValue;
      }>;
      providerOptions: typeof Options;
    }>,
  ],
  "type"
> = z.discriminatedUnion("type", [
  z.object({
    type: z.literal("reasoning"),
    text: z.string(),
    providerOptions: Options,
  }),
  z.object({
    type: z.literal("tool-call"),
    toolCallId: z.string(),
    toolName: z.string(),
    input: JsonObject,
    providerExecuted: z.literal(true),
    providerOptions: Options,
  }),
  z.object({
    type: z.literal("tool-result"),
    toolCallId: z.string(),
    toolName: z.string(),
    output: z.object({ type: z.literal("json"), value: JsonValue }),
    providerOptions: Options,
  }),
]);
export type ReplayPart = z.infer<typeof ReplayPart>;
