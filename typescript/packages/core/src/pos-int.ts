/**
 * `PosInt` (spec/api.json, `urn:threads:schema:events:v1#/$defs/PosInt`): a whole number above
 * zero. The one check a public entry point makes on such an option, before anything is written.
 *
 * A public argument is `unknown` until this says otherwise: the declared `number` is only what
 * the checker sees, and `NaN`, `Infinity` and a fraction are numbers the writer would reject far
 * later, as a throw. Zero is not positive, so a zero timeout is a refusal, never "immediate".
 */
export function isPosInt(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value > 0;
}
