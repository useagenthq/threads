import { z } from "zod";
import { ArtifactRef } from "../common";
import { InputPart } from "../content";
import type { Arr, Strict } from "../zod-types";

// "Exactly one of two keys" as a union of two strict objects: each branch forbids the other key.

export type TextOrRef<S extends z.core.$ZodLooseShape> = z.ZodUnion<
  readonly [
    Strict<S & { text: z.ZodString }>,
    Strict<S & { ref: typeof ArtifactRef }>,
  ]
>;

export function textOrRef<S extends z.core.$ZodLooseShape>(
  shape: S,
): TextOrRef<S> {
  return z.union([
    z.strictObject({ ...shape, text: z.string() }),
    z.strictObject({ ...shape, ref: ArtifactRef }),
  ]);
}

export type TextOrContent<S extends z.core.$ZodLooseShape> = z.ZodUnion<
  readonly [
    Strict<S & { text: z.ZodString }>,
    Strict<S & { content: Arr<typeof InputPart> }>,
  ]
>;

export function textOrContent<S extends z.core.$ZodLooseShape>(
  shape: S,
): TextOrContent<S> {
  return z.union([
    z.strictObject({ ...shape, text: z.string() }),
    z.strictObject({ ...shape, content: z.array(InputPart).min(1) }),
  ]);
}
