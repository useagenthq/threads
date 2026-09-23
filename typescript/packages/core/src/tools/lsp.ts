import { posix } from "node:path";
import { z } from "zod";
import { ConfigError } from "../agent/errors";
import { assertNever } from "../assert-never";
import type { ToolRun } from "../loop/types";
import { execute, toolRunOf } from "../sandbox/exec";
import { type Builtin, builtin, done, sessionOf } from "./builtin";
import { LSP_DRIVER } from "./lsp-driver";
import { LspInput } from "./sandbox-inputs";

// lsp: read_only, run in the sandbox against the language servers the image
// has, one per declared language. A server that is missing or not ready answers `unavailable`,
// never an empty success.

type Server = {
  readonly command: readonly string[];
  readonly extensions: Readonly<Record<string, string>>;
};
type Operation = z.infer<typeof LspInput>["operation"];

/** The preset servers per language: the argv and the LSP languageId per file extension. */
export const LSP_SERVERS: Readonly<Record<string, Server>> = {
  typescript: {
    command: ["typescript-language-server", "--stdio"],
    extensions: {
      ".ts": "typescript",
      ".tsx": "typescriptreact",
      ".js": "javascript",
      ".jsx": "javascriptreact",
    },
  },
  python: {
    command: ["pyright-langserver", "--stdio"],
    extensions: { ".py": "python" },
  },
  go: { command: ["gopls"], extensions: { ".go": "go" } },
  rust: { command: ["rust-analyzer"], extensions: { ".rs": "rust" } },
};

const READY_S = 30;
const WAIT_S = 20;
const TIMEOUT_MS = (READY_S + WAIT_S + 10) * 1000;

// The driver's one line and the server's answers are sandbox output: parsed at the boundary.
const Answer = z.union([
  z.object({ ok: z.json() }),
  z.object({ unavailable: z.string() }),
]);
const Position = z.object({ line: z.int(), character: z.int() });
const Range = z.object({ start: Position, end: Position });
const Location = z.object({ uri: z.string(), range: Range });
const Link = z.object({ targetUri: z.string(), targetSelectionRange: Range });
const Diagnostic = z.object({
  range: Range,
  severity: z.int().optional(),
  message: z.string(),
});
type DocSymbol = {
  readonly name: string;
  readonly kind: number;
  readonly range?: z.infer<typeof Range> | undefined;
  readonly location?: z.infer<typeof Location> | undefined;
  readonly children?: readonly DocSymbol[] | undefined;
};
const DocSymbol: z.ZodType<DocSymbol> = z.lazy(() =>
  z.object({
    name: z.string(),
    kind: z.int(),
    range: Range.optional(),
    location: Location.optional(),
    children: z.array(DocSymbol).optional(),
  }),
);
const Marked = z.union([z.string(), z.object({ value: z.string() })]);
const Hover = z.object({ contents: z.union([Marked, z.array(Marked)]) });

const SEVERITY = ["", "error", "warning", "info", "hint"];
const at = (uri: string, p: z.infer<typeof Position>): string =>
  `${uri.replace(/^file:\/\//, "")}:${p.line + 1}:${p.character + 1}`;
const listed = (found: readonly string[], none: string): string =>
  found.length === 0 ? none : found.join("\n");

function outline(list: readonly DocSymbol[], depth = 0): string[] {
  return list.flatMap((s) => {
    const start = s.range?.start ?? s.location?.range.start;
    const line = start === undefined ? "" : ` line ${start.line + 1}`;
    return [
      `${"  ".repeat(depth)}${s.name} (kind ${s.kind})${line}`,
      ...outline(s.children ?? [], depth + 1),
    ];
  });
}

function diagnostics(uri: string, result: unknown): string | undefined {
  const d = z.array(Diagnostic).safeParse(result);
  if (!d.success) return undefined;
  const line = (x: z.infer<typeof Diagnostic>): string =>
    `${at(uri, x.range.start)} ${SEVERITY[x.severity ?? 1] ?? "info"}: ${x.message}`;
  return listed(d.data.map(line), "no diagnostics");
}

function locations(op: string, result: unknown): string | undefined {
  const one = z.union([Location, Link]);
  const got = z.union([z.null(), one, z.array(one)]).safeParse(result);
  if (!got.success) return undefined;
  const all = got.data === null ? [] : [got.data].flat();
  const where = (l: z.infer<typeof one>): string =>
    "uri" in l
      ? at(l.uri, l.range.start)
      : at(l.targetUri, l.targetSelectionRange.start);
  return listed(all.map(where), `no ${op} found`);
}

function hover(result: unknown): string | undefined {
  const h = z.union([z.null(), Hover]).safeParse(result);
  if (!h.success) return undefined;
  if (h.data === null) return "no hover information";
  return [h.data.contents]
    .flat()
    .map((p) => (typeof p === "string" ? p : p.value))
    .join("\n");
}

/** The model-visible text of an LSP answer, or undefined when it isn't the expected shape. */
export function lspText(
  operation: Operation,
  uri: string,
  result: unknown,
): string | undefined {
  switch (operation) {
    case "diagnostics":
      return diagnostics(uri, result);
    case "definition":
    case "references":
      return locations(operation, result);
    case "hover":
      return hover(result);
    case "symbols": {
      const s = z.union([z.null(), z.array(DocSymbol)]).safeParse(result);
      return s.success
        ? listed(outline(s.data ?? []), "no symbols")
        : undefined;
    }
    default:
      return assertNever(operation);
  }
}

/** The driver's printed answer as the tool's result. */
export function answered(
  operation: Operation,
  path: string,
  stdout: string,
): ToolRun {
  let raw: unknown;
  try {
    raw = JSON.parse(stdout.trim().split("\n").at(-1) ?? "");
  } catch {
    return done("unavailable: the lsp client printed no answer", true);
  }
  const answer = Answer.safeParse(raw);
  if (!answer.success)
    return done("unavailable: the lsp client answered malformed output", true);
  if ("unavailable" in answer.data)
    return done(`unavailable: ${answer.data.unavailable}`, true);
  const shown = lspText(operation, `file://${path}`, answer.data.ok);
  return shown === undefined
    ? done(`unavailable: an unexpected ${operation} answer`, true)
    : done(shown);
}

function servers(languages: readonly string[]): readonly Server[] {
  return languages.map((name) => {
    const server = LSP_SERVERS[name];
    if (server === undefined)
      throw new ConfigError(
        "unknown_preset",
        `lsp: no language server preset ${name}; known: ${Object.keys(LSP_SERVERS).join(", ")}`,
      );
    return server;
  });
}

/** lsp for the declared languages; an unknown one is a setup error. */
export function lsp(languages: readonly string[]): Builtin {
  return lspWith(servers(languages));
}

/** lsp over these servers. */
export function lspWith(declared: readonly Server[]): Builtin {
  return builtin({
    name: "lsp",
    input: LspInput,
    effect: "read_only",
    run: async (input, ctx, env) => {
      const path = posix.resolve("/workspace", input.path);
      const ext = posix.extname(path);
      const server = declared.find((s) => s.extensions[ext] !== undefined);
      if (server === undefined)
        return done(
          `unavailable: no declared language server handles ${path}`,
          true,
        );
      const positioned =
        input.operation !== "diagnostics" && input.operation !== "symbols";
      if (
        positioned &&
        (input.line === undefined || input.character === undefined)
      )
        return done(`${input.operation} needs line and character`, true);
      const session = await sessionOf(env);
      if (!session.ok) return session.error;
      const args = {
        command: server.command,
        root: "/workspace",
        path,
        language_id: server.extensions[ext],
        operation: input.operation,
        line: input.line ?? 1,
        character: input.character ?? 1,
        ready_s: READY_S,
        wait_s: WAIT_S,
      };
      const ran = await execute(
        session.value,
        ["python3", "-c", LSP_DRIVER, JSON.stringify(args)],
        env.context,
        { env: {}, timeoutMs: TIMEOUT_MS, processKey: ctx.effectKey },
        env.artifacts,
        1 << 20,
      );
      if (!ran.ok) return toolRunOf(ran);
      if (ran.value.exit_code !== 0)
        return done(
          `unavailable: the lsp client failed: ${ran.value.stderr}`,
          true,
        );
      return answered(input.operation, path, ran.value.stdout);
    },
  });
}
