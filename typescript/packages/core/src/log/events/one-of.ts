import { z } from "zod";
import { type Ruled, withRule } from "../rules";

// "Exactly one of two keys". The data object lists both keys as optional and adds one of these.

const TEXT_OR_REF = {
  oneOf: [{ required: ["text"] }, { required: ["ref"] }],
} as const;
export const TextOrRef: Ruled<z.ZodUnknown, typeof TEXT_OR_REF> = withRule(
  z.unknown(),
  TEXT_OR_REF,
  {
    id: "TextOrRef",
    description: "Exactly one of inline text or an artifact ref.",
  },
);

const TEXT_OR_CONTENT = {
  oneOf: [{ required: ["text"] }, { required: ["content"] }],
} as const;
export const TextOrContent: Ruled<z.ZodUnknown, typeof TEXT_OR_CONTENT> =
  withRule(z.unknown(), TEXT_OR_CONTENT, {
    id: "TextOrContent",
    description: "Exactly one of plain text or ordered input parts.",
  });

/** The rule a data object with optional `text` and `ref` adds. */
export const HAS_TEXT_OR_REF: {
  readonly allOf: readonly [{ readonly $ref: typeof TextOrRef }];
} = { allOf: [{ $ref: TextOrRef }] };

/** The rule a data object with optional `text` and `content` adds. */
export const HAS_TEXT_OR_CONTENT: {
  readonly allOf: readonly [{ readonly $ref: typeof TextOrContent }];
} = { allOf: [{ $ref: TextOrContent }] };
