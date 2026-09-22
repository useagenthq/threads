import type { z } from "zod";

// isolatedDeclarations needs every exported schema annotated. These aliases keep the
// annotations short; the Zod type is still the one source of the TS type.

export type Strict<S extends z.core.$ZodLooseShape> = z.ZodObject<
  S,
  z.core.$strict
>;
export type Opt<T extends z.core.SomeType> = z.ZodOptional<T>;
export type Arr<T extends z.core.SomeType> = z.ZodArray<T>;
export type Lit<T extends z.core.util.Literal> = z.ZodLiteral<T>;
export type EnumOf<T extends readonly string[]> = z.ZodEnum<{
  [K in T[number]]: K;
}>;
export type Brand<
  T extends z.core.SomeType,
  B extends string,
> = z.core.$ZodBranded<T, B>;
