// shell commands tokenized for rule matching only. The sandbox stays the
// security boundary; this parser only has to make rules conservative.

/** One simple command, as rules see it. */
export type SimpleCommand = {
  readonly tokens: readonly string[];
  /**
   * False after a dangerous leading assignment, or when the command opens with or follows a
   * reserved word (`if`, `do`, `!`, ...): such a command never matches an allow rule.
   */
  readonly allowable: boolean;
};

/**
 * A command line split into simple commands. An unparseable line holds a construct the lexer
 * doesn't model: its commands are a best effort that deny and ask rules still read, its raw
 * `words` are read too, and no allow rule matches it.
 */
export type ParsedShell = {
  readonly commands: readonly SimpleCommand[];
  readonly unparseable: boolean;
  readonly words: readonly string[];
};

const DANGEROUS_ENV =
  /^(PATH|LD_\w*|DYLD_\w*|BASH_ENV|ENV|IFS|PYTHONPATH|NODE_OPTIONS|PS4)$/;
const ASSIGNMENT = /^([A-Za-z_]\w*)=/;
// Words of an unparseable command: what a deny rule may still find.
const RAW_WORD = /[^\s;&|()<>`$"'{}\\]+/g;
// Reserved words that can open a simple command without being it. The command after one is
// read too, so a deny rule sees `rm` in `if true; then rm -rf /; fi`.
const KEYWORDS = new Set([
  "!",
  "{",
  "}",
  "if",
  "then",
  "elif",
  "else",
  "fi",
  "while",
  "until",
  "do",
  "done",
  "esac",
  "coproc",
]);
const WRAPPERS = new Set(["timeout", "nice", "nohup", "time"]);
// Wrapper options whose value is the next word: timeout's signal and kill-after, nice's niceness.
const OPTIONS_WITH_VALUE = new Set([
  "-s",
  "-k",
  "-n",
  "--signal",
  "--kill-after",
  "--adjustment",
]);
// Commands whose words run as code the parser doesn't read.
const OPAQUE = new Set(["eval", "exec", "function"]);
// One lexer step: blanks, a separator, a modelled word piece (bare text, a single-quoted string,
// or a double-quoted one holding no escape or expansion), a group edge (a subshell's or a
// substitution's parenthesis, a backtick), or anything else (a double-quoted string with an
// escape or expansion, an escape, or one character such as `$`, `>`, `{` or `#`). A group edge
// ends the simple command, so the command inside is read on its own; a group edge or anything
// else makes the line unparseable.
const PIECE =
  /([ \t]+)|([;&|\n])|('[^']*'|"[^"\\$`]*"|[^\s'";&|\\$`(){}<>#]+)|([()`])|("(?:[^"\\]|\\[\s\S])*"|\\[\s\S]?|[\s\S])/g;

/** POSIX-quoted words, split into simple commands on ; && || | &, newlines and group edges. */
export function parseShell(command: string): ParsedShell {
  const { lines, odd } = lex(command);
  const commands: SimpleCommand[] = [];
  let unparseable = odd;
  for (const words of lines) {
    const simple = simplify(words);
    unparseable ||= opaque(simple.tokens);
    commands.push(...unwrapKeywords(simple));
  }
  return { commands, unparseable, words: command.match(RAW_WORD) ?? [] };
}

/**
 * Words per simple command, exact for the subset it models: bare words, single quotes
 * (literal), and double quotes holding no escape or expansion. Anything else is read as well as
 * the lexer can and flagged odd.
 */
function lex(command: string): {
  readonly lines: readonly (readonly string[])[];
  readonly odd: boolean;
} {
  const lines: string[][] = [[]];
  let word: string | undefined;
  let odd = false;
  const end = (): void => {
    if (word !== undefined) lines.at(-1)?.push(word);
    word = undefined;
  };
  for (const [, blank, separator, piece, edge, other] of command.matchAll(
    PIECE,
  )) {
    if (piece !== undefined) word = (word ?? "") + unquote(piece);
    else if (blank !== undefined) end();
    else if (separator !== undefined || edge !== undefined) {
      odd ||= edge !== undefined;
      end();
      lines.push([]);
    } else {
      odd = true;
      word = (word ?? "") + readEscaped(other ?? "");
    }
  }
  end();
  return { lines, odd };
}

/** Quotes are removed; the quoted text joins the word. */
function unquote(piece: string): string {
  const q = piece[0];
  return q === "'" || q === '"' ? piece.slice(1, -1) : piece;
}

/** An escape gives its character (a line continuation gives nothing), as the shell reads it. */
function readEscaped(piece: string): string {
  if (piece.startsWith("\\")) return piece.slice(1).replace("\n", "");
  if (piece.length > 1 && piece.startsWith('"'))
    return piece
      .slice(1, -1)
      .replace(/\\([\\$`"\n])/g, (_, c: string) => (c === "\n" ? "" : c));
  return piece;
}

/** Strips leading assignments and wrappers, in any order. */
function simplify(words: readonly string[]): SimpleCommand {
  let rest = words;
  let allowable = true;
  for (;;) {
    const name = ASSIGNMENT.exec(rest[0] ?? "");
    const skip = name === null ? wrapperLength(rest) : 1;
    if (skip === 0) return { tokens: rest, allowable };
    allowable &&= !DANGEROUS_ENV.test(name?.[1] ?? "");
    rest = rest.slice(skip);
  }
}

/**
 * How many leading words are a `timeout [options] N`, `nice`, `nohup` or `time` wrapper,
 * options and a closing `--` included.
 */
function wrapperLength(words: readonly string[]): number {
  const [head] = words;
  if (head === undefined || !WRAPPERS.has(head)) return 0;
  let n = 1;
  for (let w = words[n]; w?.startsWith("-"); w = words[n]) {
    if (w === "--") {
      n += 1;
      break;
    }
    n += OPTIONS_WITH_VALUE.has(w) ? 2 : 1;
  }
  return head === "timeout" ? n + 1 : n;
}

function opaque(tokens: readonly string[]): boolean {
  return (
    OPAQUE.has(tokens[0] ?? "") || tokens.some((w) => w === "{" || w === "}")
  );
}

/**
 * A command that opens with a reserved word, and the command after the word, both as deny and
 * ask rules read them. Neither is allowed.
 */
function unwrapKeywords(simple: SimpleCommand): readonly SimpleCommand[] {
  const [first] = simple.tokens;
  if (first === undefined) return [];
  if (!KEYWORDS.has(first)) return [simple];
  const inner = unwrapKeywords(simplify(simple.tokens.slice(1)));
  return [simple, ...inner].map((c) => ({ ...c, allowable: false }));
}

/** Whitespace-separated words of a rule specifier (quotes removed); none if it isn't modelled. */
export function specTokens(spec: string): readonly string[] {
  const { lines, odd } = lex(spec);
  return odd ? [] : lines.flat();
}
