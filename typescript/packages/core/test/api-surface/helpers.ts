/**
 * Type-level checks that generated/surface.ts (spec/tools/gen_api_surface.py) is built from.
 * Each generated line is an Assert over one spec/api.json member: tsc fails the line when the
 * package disagrees with the contract, and fails a listed gap's line once the gap is fixed.
 */

export type Assert<T extends true> = T;

export type Equals<A, B> =
  (<T>() => T extends A ? 1 : 2) extends <T>() => T extends B ? 1 : 2
    ? true
    : false;

export type HasKey<T, K extends PropertyKey> = K extends keyof T ? true : false;

/**
 * A function or method must be callable: a property that merely exists is not one, and
 * `never` (which extends every type) isn't either.
 */
export type IsCallable<F> = [NonNullable<F>] extends [never]
  ? false
  : [NonNullable<F>] extends [(...args: never) => unknown]
    ? true
    : false;

export type IsMissing<T, K extends PropertyKey> = K extends keyof T
  ? false
  : true;

/** true when K is a property of T that can't be omitted. */
export type Req<T, K extends PropertyKey> = K extends keyof T
  ? Record<never, never> extends Pick<T, K>
    ? false
    : true
  : false;

/**
 * A function's last eight overloads, each as its whole signature. With fewer than eight, tsc
 * fills the leading slots with copies of the first overload, so every overload is in the window.
 */
type Signatures<F> = F extends {
  (...args: infer A1): infer R1;
  (...args: infer A2): infer R2;
  (...args: infer A3): infer R3;
  (...args: infer A4): infer R4;
  (...args: infer A5): infer R5;
  (...args: infer A6): infer R6;
  (...args: infer A7): infer R7;
  (...args: infer A8): infer R8;
}
  ? [
      (...args: A1) => R1,
      (...args: A2) => R2,
      (...args: A3) => R3,
      (...args: A4) => R4,
      (...args: A5) => R5,
      (...args: A6) => R6,
      (...args: A7) => R7,
      (...args: A8) => R8,
    ]
  : never;

/**
 * Every overload of F is in the window, so the option checks below see all of them. The type
 * system can't count overloads; what it shows is the window: when its first four slots are the
 * same signature, they are tsc's padding (so F has at most five overloads) or overloads F
 * declares identically, which the window shows too. Only four consecutive identical overload
 * signatures (parameters and return type) followed by more overloads can hide one; that case is
 * out of scope (spec/schema/README.md, "What it doesn't check"). Otherwise the leading slots
 * differ and this fails.
 */
export type EveryOverloadSeen<F> =
  Signatures<F> extends [infer S1, infer S2, infer S3, infer S4, ...unknown[]]
    ? [Equals<S1, S2>, Equals<S2, S3>, Equals<S3, S4>] extends [
        true,
        true,
        true,
      ]
      ? true
      : false
    : false;

/** The options object at parameter N of one overload; an overload without it has none. */
type OptionsAt<A, N extends number> = A extends readonly unknown[]
  ? [Exclude<A[N], undefined>] extends [never]
    ? Record<never, never>
    : Exclude<A[N], undefined>
  : never;

/** Every overload's options object, as a union (each member of a union options type counts). */
type EachOptions<F, N extends number> = OptionsAt<
  Parameters<Signatures<F>[number]>,
  N
>;

type AnyHas<U, K extends PropertyKey> = true extends (
  U extends unknown
    ? HasKey<U, K>
    : never
)
  ? true
  : false;

type AllRequire<U, K extends PropertyKey> = false extends (
  U extends unknown
    ? Req<U, K>
    : never
)
  ? false
  : true;

/** K is an option of any overload of F, whose options object is parameter N. */
export type OptionPresent<F, N extends number, K extends PropertyKey> = AnyHas<
  EachOptions<F, N>,
  K
>;

/** K is required by every overload of F (an overload without K doesn't require it). */
export type OptionRequired<
  F,
  N extends number,
  K extends PropertyKey,
> = AllRequire<EachOptions<F, N>, K>;
