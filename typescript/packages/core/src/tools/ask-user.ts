import type { z } from "zod";
import { AskUserInput } from "./agent-inputs";

// ask_user's matching rule (spec/schema/README.md, "Questions and remembered rules"), written
// by hand over code points so both languages agree byte for byte: trim the ECMAScript
// WhiteSpace and LineTerminator set, then fold ASCII A-Z only. No Unicode normalization.

export type Ask = z.infer<typeof AskUserInput>;

const SPACE = new Set([
  0x09, 0x0a, 0x0b, 0x0c, 0x0d, 0x20, 0xa0, 0x1680, 0x2000, 0x2001, 0x2002,
  0x2003, 0x2004, 0x2005, 0x2006, 0x2007, 0x2008, 0x2009, 0x200a, 0x2028,
  0x2029, 0x202f, 0x205f, 0x3000, 0xfeff,
]);
/** The reply separators of a multi_select answer: a comma and the line terminators. */
const SEPARATOR = /[,\n\r\u2028\u2029]/;
const DIGITS = /^[0-9]+$/;

/** Trimmed of the whitespace set, ASCII letters lowercased. */
export function norm(text: string): string {
  const points = [...text];
  const isSpace = (p: string | undefined): boolean =>
    p !== undefined && SPACE.has(p.codePointAt(0) ?? -1);
  let start = 0;
  let end = points.length;
  while (start < end && isSpace(points[start])) start++;
  while (end > start && isSpace(points[end - 1])) end--;
  return points
    .slice(start, end)
    .map((p) => (p >= "A" && p <= "Z" ? p.toLowerCase() : p))
    .join("");
}

/** Why an ask_user input can't be asked (rule 46), or undefined when it can. */
export function askProblem(ask: Ask): string | undefined {
  const { options, multi_select: multi = false } = ask;
  if (options === undefined)
    return multi ? "multi_select needs options" : undefined;
  const keys = options.map(norm);
  if (keys.some((k) => k === "")) return "an option is blank";
  const twice = keys.findIndex((k, i) => keys.indexOf(k) !== i);
  if (twice !== -1) return `option ${options[twice]} is listed twice`;
  const split = options.find((o) => SEPARATOR.test(o));
  return multi && split !== undefined
    ? `option ${split} contains a comma or a line break`
    : undefined;
}

/** A parsed ask_user input the rules accept, else undefined. */
export function askOf(input: unknown): Ask | undefined {
  const parsed = AskUserInput.safeParse(input);
  return parsed.success && askProblem(parsed.data) === undefined
    ? parsed.data
    : undefined;
}

/** Whether a reply may pick an option by its 1-based number: no option is itself a number. */
function numbered(options: readonly string[]): boolean {
  return !options.some((o) => DIGITS.test(norm(o)));
}

function pick(options: readonly string[], item: string): string | undefined {
  const key = norm(item);
  const literal = options.find((o) => norm(o) === key);
  if (literal !== undefined || !numbered(options) || !DIGITS.test(key))
    return literal;
  return options[Number(key) - 1];
}

/**
 * The recorded answer for a reply, or undefined when it is none of the options: an option's
 * offered spelling, or for multi_select the chosen options in first-reply order joined by "\n".
 * A list is taken item by item; free text is recorded as given when it isn't blank.
 */
export function matchAnswer(
  ask: Ask,
  reply: string | readonly string[],
): string | undefined {
  const items = typeof reply === "string" ? [reply] : reply;
  const { options, multi_select: multi = false } = ask;
  if (options === undefined)
    return items.some((i) => norm(i) === "") ? undefined : items.join("\n");
  const parts = multi
    ? items.flatMap((i) => i.split(SEPARATOR)).filter((i) => norm(i) !== "")
    : items;
  if (parts.length === 0 || (!multi && parts.length !== 1)) return undefined;
  const chosen = parts.map((p) => pick(options, p));
  if (chosen.some((c) => c === undefined)) return undefined;
  return [...new Set(chosen)].join("\n");
}

/** Whether a recorded answer's text is one the question accepts (rule 25). */
export function acceptsRecorded(ask: Ask, preview: string): boolean {
  const { options, multi_select: multi = false } = ask;
  if (options === undefined) return true;
  const parts = multi ? preview.split("\n") : [preview];
  return (
    parts.every((p) => options.includes(p)) &&
    new Set(parts).size === parts.length
  );
}

/** The choices as a message lists them: numbered, or bulleted when an option is a number. */
function choices(options: readonly string[]): string {
  return options
    .map((o, i) => (numbered(options) ? `${i + 1}. ${o}` : `- ${o}`))
    .join("\n");
}

function hint(ask: Ask, options: readonly string[]): string {
  const how = numbered(options) ? "the number or the text" : "the exact text";
  return ask.multi_select === true
    ? `Reply with ${how} of each choice, separated by commas.`
    : `Reply with ${how} of your choice.`;
}

/** The message a channel shows for an open question. */
export function questionText(ask: Ask): string {
  const { options } = ask;
  if (options === undefined) return ask.question;
  return `${ask.question}\n\n${choices(options)}\n\n${hint(ask, options)}`;
}

/** The message a channel shows after a reply that matched no option. */
export function correctionText(ask: Ask): string {
  const { options } = ask;
  if (options === undefined) return "Please answer with some text.";
  return `Please answer with one of:\n\n${choices(options)}\n\n${hint(ask, options)}`;
}

/** Why Thread.answer refused an answer (invalid_answer): what it accepts. */
export function invalidAnswer(ask: Ask): string {
  const { options } = ask;
  return options === undefined
    ? "answer with some text"
    : `answer one of: ${options.join(", ")}`;
}
