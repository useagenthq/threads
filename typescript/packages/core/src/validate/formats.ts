// `format` and `multipleOf` for semantic rule 20, defined exactly as spec/schema/README.md
// ("Output schemas") defines them, so the Python reader (threads/_json_formats.py) answers the
// same for every value. Character classes are spelled out: `\d` and `\s` differ between
// ECMAScript and Python regular expressions.

const DATE = /^([0-9]{4})-([0-9]{2})-([0-9]{2})$/;
const TIME =
  /^([0-9]{2}):([0-9]{2}):([0-9]{2})(\.[0-9]+)?([Zz]|[+-]([0-9]{2}):([0-9]{2}))$/;
const EMAIL = /^[^@ \t\n\r\f\v]+@[^@. \t\n\r\f\v]+(\.[^@. \t\n\r\f\v]+)+$/;
const URI = /^[A-Za-z][A-Za-z0-9+.-]*:[^ \t\n\r\f\v]*$/;
const UUID =
  /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/;
const LONG_MONTHS: ReadonlySet<number> = new Set([1, 3, 5, 7, 8, 10, 12]);
const DECIMAL = /^(-?)([0-9]+)(?:\.([0-9]+))?(?:[eE]([+-]?[0-9]+))?$/;

function monthDays(year: number, month: number): number {
  if (LONG_MONTHS.has(month)) return 31;
  if (month !== 2) return 30;
  const leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
  return leap ? 29 : 28;
}

function isDate(text: string): boolean {
  const found = DATE.exec(text);
  if (found === null) return false;
  const [year, month, day] = found.slice(1, 4).map(Number);
  if (year === undefined || month === undefined || day === undefined)
    return false;
  return month >= 1 && month <= 12 && day >= 1 && day <= monthDays(year, month);
}

function isTime(text: string): boolean {
  const found = TIME.exec(text);
  if (found === null) return false;
  const [hour, minute, second] = found.slice(1, 4).map(Number);
  const clock =
    Number(hour) <= 23 && Number(minute) <= 59 && Number(second) <= 59;
  const offHour = found[6];
  const offset =
    offHour === undefined || (Number(offHour) <= 23 && Number(found[7]) <= 59);
  return clock && offset;
}

function isDateTime(text: string): boolean {
  const sep = text.slice(10, 11);
  return (
    (sep === "T" || sep === "t") &&
    isDate(text.slice(0, 10)) &&
    isTime(text.slice(11))
  );
}

/** The formats rule 20 checks, each on a string only; any other format is unsupported. */
export const FORMATS: ReadonlyMap<string, (text: string) => boolean> = new Map<
  string,
  (text: string) => boolean
>([
  ["date", isDate],
  ["time", isTime],
  ["date-time", isDateTime],
  ["email", (text) => EMAIL.test(text)],
  ["uri", (text) => URI.test(text)],
  ["uuid", (text) => UUID.test(text)],
]);

/**
 * The number's shortest round-trip decimal as [digits, exponent]: value = digits × 10^exponent.
 * String() and Python's repr write the same digits.
 */
function decimal(n: number): readonly [bigint, number] {
  const found = DECIMAL.exec(String(n));
  if (found === null) throw new TypeError(`not a finite number: ${n}`);
  const [, sign, whole = "", fraction = "", exponent = "0"] = found;
  const digits = BigInt(whole + fraction) * (sign === "-" ? -1n : 1n);
  return [digits, Number(exponent) - fraction.length];
}

/**
 * Whether `value` is an integer multiple of `divisor`, exactly in decimal (0.3 is a multiple of
 * 0.1), never by float division.
 */
export function multipleOf(value: number, divisor: number): boolean {
  const [a, p] = decimal(value);
  const [b, q] = decimal(divisor);
  if (b <= 0n) throw new TypeError(`multipleOf must be positive: ${divisor}`);
  const low = Math.min(p, q);
  return (a * 10n ** BigInt(p - low)) % (b * 10n ** BigInt(q - low)) === 0n;
}
