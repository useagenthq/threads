import { z } from "zod";
import { withRule } from "../rules";

// "Exactly one of two keys". The data object lists both keys as optional and adds one of these.

export const TextOrRef: z.ZodUnknown = withRule(
  z.unknown(),
  { oneOf: [{ required: ["text"] }, { required: ["ref"] }] },
  {
    id: "TextOrRef",
    description: "Exactly one of inline text or an artifact ref.",
  },
);

export const TextOrContent: z.ZodUnknown = withRule(
  z.unknown(),
  { oneOf: [{ required: ["text"] }, { required: ["content"] }] },
  {
    id: "TextOrContent",
    description: "Exactly one of plain text or ordered input parts.",
  },
);
