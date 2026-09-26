import type { z } from "zod";

// isolatedDeclarations needs every exported schema annotated, so these aliases keep the
// annotations short. They mirror core's log/zod-types; the Zod object is still the one source of
// the TypeScript type. Local because @threads/core does not export them.

export type Strict<S extends z.core.$ZodLooseShape> = z.ZodObject<
  S,
  z.core.$strict
>;
export type Opt<T extends z.core.SomeType> = z.ZodOptional<T>;
export type Arr<T extends z.core.SomeType> = z.ZodArray<T>;
export type EnumOf<T extends readonly string[]> = z.ZodEnum<{
  [K in T[number]]: K;
}>;
