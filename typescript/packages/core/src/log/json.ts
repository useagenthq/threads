import { err, ok, type Result } from "../result";

// Strict JSON for log lines. JSON.parse silently keeps
// the last duplicate key and turns 1e400 into Infinity, so lines are parsed here instead.

export type JsonParseError = {
  readonly offset: number;
  readonly message: string;
};

// ponytail: fixed nesting cap so hostile input can't overflow the stack; log lines are shallow.
const MAX_DEPTH = 512;
const NUMBER = /-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?/y;
// biome-ignore lint/suspicious/noControlCharactersInRegex: JSON forbids raw control characters in strings.
const STRING = /"(?:[^"\\\u0000-\u001f]|\\["\\/bfnrt]|\\u[0-9a-fA-F]{4})*"/y;
const WHITESPACE = /[ \t\n\r]*/y;

const FAIL: unique symbol = Symbol("fail");
type Fail = typeof FAIL;

class Parser {
  pos = 0;
  error = "";
  readonly text: string;

  constructor(text: string) {
    this.text = text;
  }

  fail(message: string): Fail {
    this.error = message;
    return FAIL;
  }

  skipWhitespace(): void {
    WHITESPACE.lastIndex = this.pos;
    WHITESPACE.test(this.text);
    this.pos = WHITESPACE.lastIndex;
  }

  value(depth: number): unknown {
    if (depth > MAX_DEPTH) return this.fail("nesting too deep");
    this.skipWhitespace();
    const c = this.text[this.pos];
    if (c === "{") return this.object(depth + 1);
    if (c === "[") return this.array(depth + 1);
    if (c === '"') return this.string();
    if (c === "t") return this.literal("true", true);
    if (c === "f") return this.literal("false", false);
    if (c === "n") return this.literal("null", null);
    return this.number();
  }

  literal<T>(word: string, result: T): T | Fail {
    if (!this.text.startsWith(word, this.pos))
      return this.fail("invalid token");
    this.pos += word.length;
    return result;
  }

  number(): number | Fail {
    NUMBER.lastIndex = this.pos;
    const match = NUMBER.exec(this.text);
    if (match === null) return this.fail("invalid token");
    this.pos = NUMBER.lastIndex;
    const n = Number(match[0]);
    if (!Number.isFinite(n)) return this.fail("non-finite number");
    // An integral value the other language can't represent exactly is rejected, never rounded.
    if (Number.isInteger(n) && !Number.isSafeInteger(n))
      return this.fail("integer outside ±(2^53−1)");
    // v1 has no float-typed fields, and JCS spells every integral double as an integer, so
    // `1.0` or `1e3` is never valid: it would slip past an integer field.
    if (Number.isInteger(n) && /[.eE]/.test(match[0]))
      return this.fail("integral value not spelled as an integer");
    return n;
  }

  string(): string | Fail {
    STRING.lastIndex = this.pos;
    const match = STRING.exec(this.text);
    if (match === null) return this.fail("invalid string");
    this.pos = STRING.lastIndex;
    // The token is already validated JSON, so the platform decoder only resolves escapes.
    const decoded: unknown = JSON.parse(match[0]);
    if (typeof decoded !== "string") return this.fail("invalid string");
    if (!decoded.isWellFormed()) return this.fail("lone surrogate in string");
    return decoded;
  }

  array(depth: number): unknown[] | Fail {
    this.pos++;
    const items: unknown[] = [];
    this.skipWhitespace();
    if (this.text[this.pos] === "]") {
      this.pos++;
      return items;
    }
    for (;;) {
      const item = this.value(depth);
      if (item === FAIL) return FAIL;
      items.push(item);
      this.skipWhitespace();
      const c = this.text[this.pos++];
      if (c === "]") return items;
      if (c !== ",") return this.fail("expected , or ]");
    }
  }

  object(depth: number): Record<string, unknown> | Fail {
    this.pos++;
    const entries: [string, unknown][] = [];
    const keys = new Set<string>();
    this.skipWhitespace();
    if (this.text[this.pos] === "}") {
      this.pos++;
      return {};
    }
    for (;;) {
      const entry = this.member(depth, keys);
      if (entry === FAIL) return FAIL;
      entries.push(entry);
      this.skipWhitespace();
      const c = this.text[this.pos++];
      // fromEntries defines own properties, so a "__proto__" key stays data.
      if (c === "}") return Object.fromEntries(entries);
      if (c !== ",") return this.fail("expected , or }");
    }
  }

  member(depth: number, keys: Set<string>): [string, unknown] | Fail {
    this.skipWhitespace();
    if (this.text[this.pos] !== '"') return this.fail("expected a key");
    const key = this.string();
    if (key === FAIL) return FAIL;
    // Compared after decoding escapes, so an escaped spelling of a key collides with the plain one.
    if (keys.has(key)) return this.fail(`duplicate key ${JSON.stringify(key)}`);
    keys.add(key);
    this.skipWhitespace();
    if (this.text[this.pos++] !== ":") return this.fail("expected :");
    const value = this.value(depth);
    return value === FAIL ? FAIL : [key, value];
  }
}

/** Parses one JSON text. Rejects duplicate keys, non-finite numbers, unsafe integers and lone surrogates. */
export function parseStrictJson(text: string): Result<unknown, JsonParseError> {
  if (!text.isWellFormed())
    return err({ offset: 0, message: "lone surrogate in text" });
  const parser = new Parser(text);
  const value = parser.value(0);
  if (value === FAIL) return err({ offset: parser.pos, message: parser.error });
  parser.skipWhitespace();
  if (parser.pos !== text.length)
    return err({ offset: parser.pos, message: "trailing characters" });
  return ok(value);
}
