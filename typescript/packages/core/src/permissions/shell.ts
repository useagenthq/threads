// shell commands tokenized for rule matching only. The sandbox stays the
// security boundary; this parser only has to make rules conservative.

/** One simple command, as rules see it. */
export type SimpleCommand = {
  readonly tokens: readonly string[];
  /** A leading assignment to a variable that changes what runs: never matches an allow rule. */
  readonly dangerousEnv: boolean;
};

/** A parsed command line, or the marker that it holds a construct the parser won't follow. */
export type ParsedShell =
  | { readonly kind: "parsed"; readonly commands: readonly SimpleCommand[] }
  | { readonly kind: "unparseable"; readonly words: readonly string[] };

const DANGEROUS_ENV =
  /^(PATH|LD_\w*|DYLD_\w*|BASH_ENV|ENV|IFS|PYTHONPATH|NODE_OPTIONS|PS4)$/;
const ASSIGNMENT = /^([A-Za-z_]\w*)=/;
// Words of a command the lexer refused: what a deny rule may still find.
const RAW_WORD = /[^\s;&|()<>`$"'{}\\]+/g;
// One lexer step: blanks, a separator, a word piece (bare text, a single-quoted string, or a
// double-quoted one holding no escape or expansion), or anything else. Anything else (an
// escape, expansion, redirection, group, comment or unclosed quote) is a construct the lexer
// does not model, so the command fails closed.
const PIECE =
  /([ \t]+)|([;&|\n])|('[^']*'|"[^"\\$`]*"|[^\s'";&|\\$`(){}<>#]+)|([\s\S])/g;

/** POSIX-quoted words, split into simple commands on ; && || | & and newlines. */
export function parseShell(command: string): ParsedShell {
  const unparseable = {
    kind: "unparseable",
    words: command.match(RAW_WORD) ?? [],
  } as const;
  const split = lex(command);
  if (split === undefined) return unparseable;
  const commands: SimpleCommand[] = [];
  for (const words of split) {
    const simple = simplify(words);
    if (simple === undefined) return unparseable;
    if (simple.tokens.length > 0) commands.push(simple);
  }
  return { kind: "parsed", commands };
}

/**
 * Words per simple command, exact for the subset it accepts: bare words, single quotes
 * (literal), and double quotes holding no escape or expansion. Anything else is undefined.
 */
function lex(command: string): string[][] | undefined {
  const commands: string[][] = [[]];
  let word: string | undefined;
  const end = (): void => {
    if (word !== undefined) commands.at(-1)?.push(word);
    word = undefined;
  };
  for (const [, blank, separator, piece] of command.matchAll(PIECE)) {
    if (piece !== undefined) word = (word ?? "") + unquote(piece);
    else if (blank !== undefined) end();
    else if (separator !== undefined) {
      end();
      commands.push([]);
    } else return undefined;
  }
  end();
  return commands;
}

/** Quotes are removed; the quoted text joins the word. */
function unquote(piece: string): string {
  const q = piece[0];
  return q === "'" || q === '"' ? piece.slice(1, -1) : piece;
}

/** Strips leading assignments and wrappers; undefined for eval, exec and brace groups. */
function simplify(words: readonly string[]): SimpleCommand | undefined {
  let rest = [...words];
  let dangerousEnv = false;
  for (let name = ASSIGNMENT.exec(rest[0] ?? ""); name !== null; ) {
    dangerousEnv ||= DANGEROUS_ENV.test(name[1] ?? "");
    rest = rest.slice(1);
    name = ASSIGNMENT.exec(rest[0] ?? "");
  }
  rest = stripWrappers(rest);
  const first = rest[0];
  if (first === "eval" || first === "exec") return undefined;
  if (rest.some((w) => w === "{" || w === "}")) return undefined;
  return { tokens: rest, dangerousEnv };
}

function stripWrappers(words: string[]): string[] {
  let rest = words;
  for (;;) {
    const [first, second] = rest;
    if (first === "timeout" && second !== undefined && /^\d+/.test(second))
      rest = rest.slice(2);
    else if (first === "nice" || first === "nohup" || first === "time")
      rest = rest.slice(1);
    else return rest;
  }
}

/** Whitespace-separated words of a rule specifier (quotes removed). */
export function specTokens(spec: string): readonly string[] {
  return lex(spec)?.flat() ?? [];
}
