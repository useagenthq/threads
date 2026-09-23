// `pattern` for semantic rule 20: the portable ECMA-262 subset spec/schema/README.md defines
// ("Output schemas"), translated to an ECMAScript `u`-flag expression with the same meaning as
// the Python reader's translation (threads/_json_pattern.py). Anything outside the subset throws
// TypeError: refused at setup, failed closed by a reader. `\s` is spelled out as ASCII: without
// that, ECMAScript's `\s` also matches Unicode spaces.

const SYNTAX = new Set("\\^$.|?*+()[]{}");
const ESCAPABLE = new Set([...SYNTAX, "/", "-"]);
const CLASSES = new Set("dDwWsS");
const CONTROLS = new Set("tnvfr");
const SPACES = "\\t\\n\\v\\f\\r ";
const BOUND = /^\{([0-9]+)(,([0-9]*))?\}/;
const WORD = "[A-Za-z0-9_]";
const BOUNDARY = `(?:(?<=${WORD})(?!${WORD})|(?<!${WORD})(?=${WORD}))`;
const NOT_BOUNDARY = `(?:(?<=${WORD})(?=${WORD})|(?<!${WORD})(?!${WORD}))`;
/**
 * Limits every engine meets the same way: code points in a pattern, a quantifier's bound, and
 * how deep groups nest.
 */
export const MAX_LENGTH = 1000;
const MAX_BOUND = 1000;
const MAX_DEPTH = 32;

/**
 * The pattern, compiled with its portable meaning; TypeError outside the subset or its limits,
 * whatever the regex engine throws.
 */
export function compilePattern(pattern: string): RegExp {
  if ([...pattern].length > MAX_LENGTH)
    throw new TypeError(
      `a pattern over ${MAX_LENGTH} characters is outside the portable subset`,
    );
  const source = new Translator(pattern).run();
  try {
    return new RegExp(source, "u");
  } catch {
    // A SyntaxError, or a RangeError from an engine limit: either way outside the subset.
    throw new TypeError(`pattern '${pattern}' doesn't compile`);
  }
}

type Last = "start" | "atom" | "assert" | "quantified" | "lazy";
type Member = "none" | "char" | "set" | "dash" | "range";

/**
 * One pass over the pattern. `last` is what a quantifier would apply to: an atom may be
 * quantified once, then made lazy once; nothing else may.
 */
class Translator {
  #at = 0;
  #last: Last = "start";
  readonly #out: string[] = [];
  readonly #groups: string[] = [];

  constructor(readonly text: string) {}

  run(): string {
    while (this.#at < this.text.length) this.#token(this.#char(this.#at));
    if (this.#groups.length > 0) throw this.#refuse("an unclosed group");
    return this.#out.join("");
  }

  #char(at: number): string {
    return this.text.slice(at, at + 1);
  }

  #refuse(what: string): TypeError {
    return new TypeError(
      `pattern '${this.text}': ${what} is outside the portable subset`,
    );
  }

  #emit(text: string, last: Last, width = 1): void {
    this.#out.push(text);
    this.#last = last;
    this.#at += width;
  }

  #token(char: string): void {
    if ("*+?{".includes(char)) this.#quantifier(char);
    else if (char === "\\") this.#escape();
    else if (char === "[") this.#klass();
    else if (char === "(") this.#group();
    else this.#simple(char);
  }

  #simple(char: string): void {
    if (char === "]" || char === "}")
      throw this.#refuse(`an unescaped '${char}'`);
    if (char === ")") {
      const kind = this.#groups.pop();
      if (kind === undefined) throw this.#refuse("an unbalanced ')'");
      // A lookahead is an assertion: it can't be repeated.
      this.#emit(")", kind === "(" || kind === "(?:" ? "atom" : "assert");
      return;
    }
    if (char === "|") this.#emit("|", "start");
    else if (char === "^" || char === "$") this.#emit(char, "assert");
    else this.#emit(char, "atom");
  }

  #quantifier(char: string): void {
    if (char === "?" && this.#last === "quantified") {
      this.#emit("?", "lazy");
      return;
    }
    if (this.#last !== "atom")
      throw this.#refuse(`'${char}' after nothing to repeat`);
    if (char !== "{") {
      this.#emit(char, "quantified");
      return;
    }
    const bound = BOUND.exec(this.text.slice(this.#at));
    if (bound === null) throw this.#refuse("an unescaped '{'");
    if (
      [bound[1], bound[3]].some(
        (n) => n !== undefined && n !== "" && Number(n) > MAX_BOUND,
      )
    )
      throw this.#refuse(`a bound over ${MAX_BOUND}`);
    this.#emit(bound[0], "quantified", bound[0].length);
  }

  #group(): void {
    if (this.#groups.length >= MAX_DEPTH)
      throw this.#refuse(`groups nested over ${MAX_DEPTH} deep`);
    const kind = this.text.slice(this.#at + 1, this.#at + 3);
    if (!kind.startsWith("?")) {
      this.#groups.push("(");
      this.#emit("(", "start");
    } else if (kind === "?:" || kind === "?=" || kind === "?!") {
      this.#groups.push(`(${kind}`);
      this.#emit(`(${kind}`, "start", 3);
    } else throw this.#refuse(`the group '(${kind}'`);
  }

  #escape(): void {
    const char = this.#char(this.#at + 1);
    if (char === "s") this.#emit(`[${SPACES}]`, "atom", 2);
    else if (char === "S") this.#emit(`[^${SPACES}]`, "atom", 2);
    else if (CLASSES.has(char) || CONTROLS.has(char))
      this.#emit(`\\${char}`, "atom", 2);
    else if (char === "b" || char === "B")
      // Spelled out, as the Python reader does: engines disagree on \B at a string's edges.
      this.#emit(char === "b" ? BOUNDARY : NOT_BOUNDARY, "assert", 2);
    else if (char === "-") this.#emit("-", "atom", 2);
    else if (char !== "" && ESCAPABLE.has(char))
      this.#emit(`\\${char}`, "atom", 2);
    else throw this.#refuse(`the escape '\\${char}'`);
  }

  /**
   * A class: single characters, ranges between two single characters, the escapes above but
   * \S (its complement differs), and '-' as a character only first or last.
   */
  #klass(): void {
    let end = this.#at + 1;
    const parts = ["["];
    if (this.#char(end) === "^") {
      parts.push("^");
      end += 1;
    }
    const start = end;
    let prev: Member = "none";
    while (end < this.text.length && this.#char(end) !== "]") {
      const [text, width, kind] = this.#member(end, prev, end === start);
      parts.push(text);
      end += width;
      prev = kind;
    }
    if (end >= this.text.length || end === start)
      throw this.#refuse("an empty or unclosed class");
    parts.push("]");
    this.#emit(parts.join(""), "atom", end + 1 - this.#at);
  }

  #member(
    at: number,
    prev: Member,
    first: boolean,
  ): readonly [string, number, Member] {
    const char = this.#char(at);
    if (char === "\\") return this.#classEscape(this.#char(at + 1), prev);
    if (char === "[") throw this.#refuse("an unescaped '[' in a class");
    const after = this.#char(at + 1);
    if (char !== "-" || first || after === "]")
      // The end of a range is not the start of the next one.
      return [
        char === "-" ? "\\-" : char,
        1,
        prev === "dash" ? "range" : "char",
      ];
    if (prev !== "char")
      throw this.#refuse("a range whose ends are not single characters");
    return ["-", 1, "dash"];
  }

  /**
   * A class escape: a set (\d \D \w \W \s), never a range end; or one character, which may end
   * a range (`[A-\]]`).
   */
  #classEscape(
    escaped: string,
    prev: Member,
  ): readonly [string, number, Member] {
    const set = CLASSES.has(escaped) && escaped !== "S" && prev !== "dash";
    if (set) return [escaped === "s" ? SPACES : `\\${escaped}`, 2, "set"];
    if (escaped !== "" && (CONTROLS.has(escaped) || ESCAPABLE.has(escaped)))
      return [`\\${escaped}`, 2, prev === "dash" ? "range" : "char"];
    throw this.#refuse(`the class escape '\\${escaped}'`);
  }
}
