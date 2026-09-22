import type { z } from "zod";

// The TS type a rule proves, computed from the same rule data `holds` checks, so a parsed value's
// type narrows as the rule does (a redaction has `part`, an accepted output has `value`). Every
// case here asserts only what `holds` enforces; a keyword that proves nothing a type can say
// (`not`, `minimum`, `minProperties`) leaves the type as it is.

type Present = NonNullable<unknown>;

/** `T` narrowed by the rule `R`: the type of any value of type `T` for which `holds(R, value)`. */
export type Narrow<T, R> = T extends unknown
  ? Branch<Combine<Properties<Required<Ref<T, R>, R>, R>, R>, R>
  : never;

type Ref<T, R> = R extends { readonly $ref: infer S extends z.ZodType }
  ? T & z.output<S>
  : T;

type Required<T, R> = R extends {
  readonly required: readonly (infer K extends string)[];
}
  ? T & { [P in K]-?: P extends keyof T ? Exclude<T[P], undefined> : Present }
  : T;

type Value<T, R> = R extends { readonly const: infer C }
  ? T & C
  : R extends { readonly enum: readonly (infer E)[] }
    ? T & E
    : T;

// Pick keeps each key's optionality: a property rule applies only to a key that is present.
type Properties<T, R> = R extends {
  readonly properties: infer P extends object;
}
  ? T & {
      [K in keyof Pick<T, keyof P & keyof T>]: K extends keyof P
        ? Narrow<Value<T[K], P[K]>, P[K]>
        : never;
    }
  : Value<T, R>;

type Combine<T, R> = R extends {
  readonly allOf: infer A extends readonly unknown[];
}
  ? AllOf<Choice<T, R>, A>
  : Choice<T, R>;

type AllOf<T, A> = A extends readonly [infer First, ...infer Rest]
  ? AllOf<Narrow<T, First>, Rest>
  : T;

// A oneOf branch holds only when the others fail. Another branch that only requires one key
// fails exactly when that key is absent, so its key is `?: never` in this branch.
type OneOfEach<T, O, All> = O extends unknown
  ? Narrow<T, O> & { [K in RequiredKey<Exclude<All, O>>]?: never }
  : never;

type RequiredKey<O> = O extends {
  readonly required: readonly [infer K extends string];
}
  ? K
  : never;

type Choice<T, R> = R extends { readonly oneOf: readonly (infer O)[] }
  ? OneOfEach<T, O, O>
  : R extends { readonly anyOf: readonly (infer O)[] }
    ? Narrow<T, O>
    : T;

type Branch<T, R> = R extends { readonly if: infer I }
  ?
      | Narrow<Narrow<T, I>, Then<R>>
      | Narrow<
          Otherwise<T, I>,
          R extends { readonly else: infer E } ? E : object
        >
  : T;

type Then<R> = R extends { readonly then: infer X } ? X : object;

type IsUnion<K, All = K> = K extends unknown
  ? [All] extends [K]
    ? false
    : true
  : never;

/** The values of `T` that fail an `if` on one property: that property is present and fails. */
type Otherwise<T, I> = I extends {
  readonly properties: infer P extends object;
}
  ? [keyof P] extends [infer K extends keyof P & keyof T]
    ? true extends IsUnion<K>
      ? T
      : T & { [Q in K]-?: Fails<Exclude<T[Q], undefined>, P[Q]> }
    : T
  : T;

type Fails<V, R> = R extends { readonly const: infer C }
  ? Exclude<V, C>
  : R extends { readonly enum: readonly (infer E)[] }
    ? Exclude<V, E>
    : R extends { readonly properties: object }
      ? Otherwise<V, R>
      : V;
