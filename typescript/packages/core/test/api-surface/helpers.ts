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
 * The parameter lists of a function's last eight overloads. With fewer than eight, tsc fills the
 * leading slots with copies of the first overload, so every overload is in the window.
 */
type Signatures<F> = F extends {
  (...args: infer A1): unknown;
  (...args: infer A2): unknown;
  (...args: infer A3): unknown;
  (...args: infer A4): unknown;
  (...args: infer A5): unknown;
  (...args: infer A6): unknown;
  (...args: infer A7): unknown;
  (...args: infer A8): unknown;
}
  ? [A1, A2, A3, A4, A5, A6, A7, A8]
  : never;

/**
 * At most five overloads: then the first four slots are copies of the first overload, and the
 * window holds every overload, so the option checks below see all of them. With more, the
 * leading slots differ and this fails. (It can't tell four identical consecutive overloads from
 * padding: a function declaring the same signature four times in a row is out of scope.)
 */
export type AtMostFiveOverloads<F> =
  Signatures<F> extends [infer A1, infer A2, infer A3, infer A4, ...unknown[]]
    ? [Equals<A1, A2>, Equals<A2, A3>, Equals<A3, A4>] extends [
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
type EachOptions<F, N extends number> = OptionsAt<Signatures<F>[number], N>;

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
