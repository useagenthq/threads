import { sha256Hex } from "../../../src/hash";
import type { ManifestEntry } from "../../../src/sandbox";
import {
  archiveOf,
  extract,
  type MemoryEntry,
} from "../../../src/sandbox/fake-trees";
import type { Sinks, Started } from "../../../src/sandbox/remote";
import { FILE_EXIT, WORKSPACE } from "../../../src/sandbox/remote/scripts";

// An emulated sandbox VM for the provider mocks: it runs exactly the remote kit's scripts
// (recognized by their `: threads-<kind>` first line) and a few test commands, so a mocked
// provider backend behaves like a real one without a network.

/** One file: its bytes, or only its manifest entry when seeded from a scripted snapshot. */
export type FileRec = {
  readonly mode: number;
  readonly size: number;
  readonly sha256: string;
  readonly bytes?: Uint8Array;
};

type Proc = { readonly kill: () => void };

const utf8 = new TextEncoder();
const text = new TextDecoder();

export function fileOf(bytes: Uint8Array, mode = 0o644): FileRec {
  return { mode, size: bytes.length, sha256: sha256Hex(bytes), bytes };
}

/**
 * The bytes of the files conformance snapshot scripts name only by hash (spec/tools/fixtures
 * pieces.py README_BYTES), so an export of a scripted restore can carry them.
 */
const KNOWN = new Map(
  ["# demo\n"].map((body) => [sha256Hex(utf8.encode(body)), utf8.encode(body)]),
);

/** The single-quoted words the kit writes (`'a'\''b'`), unquoted. */
export function shellWords(line: string): string[] {
  return (line.match(/(?:'[^']*'|\\[\s\S]|[^\s'\\])+/g) ?? []).map((word) =>
    word.replace(/'([^']*)'|\\([\s\S])/g, "$1$2"),
  );
}

export type ExecCall = {
  readonly cwd: string;
  readonly stdin: string;
  readonly env: Readonly<Record<string, string>>;
  readonly argv: readonly string[];
};

/** Reads an exec script back into what it runs. */
export function parseExec(script: string): ExecCall {
  const cwd = shellWords(script.split("\n")[1] ?? "")[1] ?? "";
  const input = /\nexec 0<('(?:[^']|'\\'')*')/.exec(script)?.[1] ?? "";
  const cmd = /\n__t_x=('(?:[^']|'\\'')*')\n/.exec(script)?.[1];
  const words = shellWords(script.slice(script.indexOf("\nexec env -i ") + 13));
  const at = words.indexOf(`"$__t_c"`);
  words[at] = shellWords(cmd ?? "")[0] ?? "";
  const env = Object.fromEntries(
    words.slice(0, at).map((w) => {
      const eq = w.indexOf("=");
      return [w.slice(0, eq), w.slice(eq + 1)];
    }),
  );
  return {
    cwd,
    stdin: shellWords(input)[0] ?? "",
    env,
    argv: words.slice(at),
  };
}

export class Machine {
  readonly files: Map<string, FileRec> = new Map();
  /** Symlinks by absolute path: their targets. */
  readonly links: Map<string, string> = new Map();
  /** Running processes by the key the provider recorded them under. */
  readonly procs: Map<string, Proc> = new Map();
  /** Every script it ran, for assertions (the credential canary reads these). */
  readonly scripts: string[] = [];

  constructor(readonly id: string) {}

  start(script: string, sinks: Sinks, processKey = ""): Started {
    this.scripts.push(script);
    const [head = "", ...rest] = script.split("\n");
    const tag = shellWords(head);
    const done = (exit: number, out = "", err = ""): Started => {
      if (out !== "") sinks.stdout(utf8.encode(out));
      if (err !== "") sinks.stderr(utf8.encode(err));
      return { exit: Promise.resolve(exit) };
    };
    switch (tag[1]) {
      case "threads-init":
        return done(0);
      case "threads-exec":
        return this.exec(parseExec(script), sinks, processKey);
      case "threads-export-tree":
        return { exit: this.export(sinks) };
      case "threads-import-tree":
        return { exit: this.import(tag[2] ?? "") };
      case "threads-check-write":
        return done(this.checkWrite(tag[2] ?? ""));
      case "threads-check-read":
        return done(this.checkRead(tag[2] ?? ""));
      default:
        return done(127, "", `unknown script: ${head} ${rest.length}\n`);
    }
  }

  /** `tar -cf - -C /workspace .`: the files (seeded ones by their known bytes) and links. */
  private async export(sinks: Sinks): Promise<number> {
    // A running writer changes the tree while anyone looks.
    if (this.procs.has("writer"))
      this.write(`${WORKSPACE}/tick`, utf8.encode(`${this.ticks++}`));
    const inside = (p: string) => p.startsWith(`${WORKSPACE}/`);
    const rel = (p: string) => p.slice(WORKSPACE.length + 1);
    const entries: MemoryEntry[] = [
      ...[...this.files]
        .filter(([p]) => inside(p))
        .map(([p, f]) => {
          const bytes = f.bytes ?? KNOWN.get(f.sha256);
          if (bytes === undefined) throw new Error(`no bytes for ${p}`);
          return { path: rel(p), mode: f.mode, bytes };
        }),
      ...[...this.links]
        .filter(([p]) => inside(p))
        .map(([p, target]) => ({ path: rel(p), target })),
    ];
    sinks.stdout(await archiveOf(entries));
    return 0;
  }

  /** `tar -xpf <path> -C /workspace`, then the upload removed; 2 when tar refuses it. */
  private async import(path: string): Promise<number> {
    const data = this.files.get(path)?.bytes;
    this.files.delete(path);
    const got = await extract(
      (async function* () {
        if (data !== undefined) yield data;
      })(),
    );
    if (!got.ok) return 2;
    for (const e of got.value) {
      const at = `${WORKSPACE}/${e.path}`;
      if ("target" in e) this.links.set(at, e.target);
      else this.files.set(at, fileOf(e.bytes, e.mode));
    }
    return 0;
  }

  private isDir(path: string): boolean {
    return this.files.keys().some((p) => p.startsWith(`${path}/`));
  }

  private checkWrite(path: string): number {
    return this.isDir(path) ? FILE_EXIT.is_directory : 0;
  }

  private checkRead(path: string): number {
    if (this.isDir(path)) return FILE_EXIT.is_directory;
    return this.files.has(path) ? 0 : FILE_EXIT.not_found;
  }

  private ticks = 0;

  /**
   * The test commands: echo, printenv, cat (stdin), bytes <out hex> <err hex>, fail, write <path> <text>, sleep (runs
   * until killed), spawn (sleeps, and leaves a descendant the provider doesn't track) and
   * writer (keeps writing the tree).
   */
  private exec(call: ExecCall, sinks: Sinks, key: string): Started {
    const [name = "", ...args] = call.argv;
    const out = (s: string) => sinks.stdout(utf8.encode(s));
    const input = this.files.get(call.stdin)?.bytes ?? new Uint8Array();
    this.files.delete(call.stdin);
    switch (name) {
      case "echo":
        out(`${args.join(" ")}\n`);
        return { exit: Promise.resolve(0) };
      case "printenv":
        out(
          Object.entries(call.env)
            .map(([k, v]) => `${k}=${v}\n`)
            .join(""),
        );
        return { exit: Promise.resolve(0) };
      case "cat":
        sinks.stdout(input);
        return { exit: Promise.resolve(0) };
      case "bytes":
        // bytes <stdout hex> <stderr hex>: exactly those bytes on each stream.
        sinks.stdout(Uint8Array.fromHex(args[0] ?? ""));
        sinks.stderr(Uint8Array.fromHex(args[1] ?? ""));
        return { exit: Promise.resolve(0) };
      case "fail":
        sinks.stderr(utf8.encode("boom\n"));
        return { exit: Promise.resolve(3) };
      case "write":
        this.files.set(
          `${call.cwd}/${args[0] ?? ""}`,
          fileOf(utf8.encode(args[1] ?? "")),
        );
        return { exit: Promise.resolve(0) };
      case "sleep":
        return this.sleep(key);
      case "spawn":
        this.sleep("descendant");
        return this.sleep(key);
      case "writer":
        return this.sleep("writer");
      default:
        sinks.stderr(utf8.encode(`${name}: command not found\n`));
        return { exit: Promise.resolve(127) };
    }
  }

  /** A process that runs until it is killed by its key. */
  private sleep(key: string): Started {
    const exit = Promise.withResolvers<number>();
    this.procs.set(key, { kill: () => exit.resolve(137) });
    return { exit: exit.promise };
  }

  /** The provider's kill of the process it recorded under `key`. */
  stop(key: string): void {
    this.procs.get(key)?.kill();
    this.procs.delete(key);
  }

  write(path: string, data: Uint8Array): void {
    this.files.set(path, fileOf(data.slice()));
  }

  read(path: string): Uint8Array {
    const bytes = this.files.get(path)?.bytes;
    if (bytes === undefined) throw new Error(`no file ${path}`);
    return bytes.slice();
  }

  /** The machine's files and scripts as text, for the credential canary. */
  everything(): string {
    const files = [...this.files.values()].map((f) =>
      text.decode(f.bytes ?? new Uint8Array()),
    );
    return [...this.scripts, ...files].join("\n");
  }
}

/** Seeds a machine's tree from a scripted snapshot's manifest (no bytes). */
export function seeded(
  id: string,
  manifest: readonly ManifestEntry[],
): Machine {
  const machine = new Machine(id);
  for (const e of manifest)
    machine.files.set(`${WORKSPACE}/${e.path}`, {
      mode: e.mode,
      size: e.size,
      sha256: e.sha256,
    });
  return machine;
}
