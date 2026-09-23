import { sha256Hex } from "../hash";
import { canonicalize, SandboxId, SnapshotId } from "../log";
import type { LookupResult } from "../model/protocol";
import { err, ok, type Result } from "../result";
import type {
  ExecOutput,
  FileFailure,
  Sandbox,
  SandboxInfo,
  SandboxSession,
  SnapshotData,
} from "./protocol";
import { SandboxScript } from "./script";

// fakeSandbox() (spec/api.json, ): an in-memory provider for tests. Files live in
// a Map; exec runs only the tools sandbox.json scripts (the command's first word names one);
// snapshots copy the tree and hash its manifest; scripted snapshots restore as the script says.

type Tree = Map<string, Uint8Array>;
type Captured = { readonly tree: Tree; readonly data: SnapshotData };
/** What an operation key created, for lookup after a lost response. */
type Operation =
  | { readonly kind: "sandbox"; readonly id: string }
  | { readonly kind: "snapshot"; readonly id: string }
  | { readonly kind: "unsupported" };

/** The fake plus what it saw: `creates` counts provider create and restore calls. */
export type FakeSandbox = Sandbox & { readonly creates: () => number };

const INFO: SandboxInfo = {
  provider: "fake",
  egress: "enforced",
  capture_classes: ["filesystem"],
  browser: "none",
  desktop: "none",
  lookup: { create: "final", snapshot: "final" },
  termination: "confirmed",
};
const CHUNK = 4096;
const utf8 = new TextEncoder();

/** RFC 8785 hash of the tree's manifest: path, mode, size, sha256. */
export function manifestHash(tree: ReadonlyMap<string, Uint8Array>): string {
  const manifest = [...tree.entries()]
    .toSorted(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
    .map(([path, bytes]) => ({
      path,
      mode: 0o644,
      size: bytes.length,
      sha256: sha256Hex(bytes),
    }));
  const text = canonicalize(manifest);
  if (!text.ok) throw new Error("a manifest is JSON");
  return sha256Hex(text.value);
}

/** An absolute path with `.` and `..` resolved, relative to /workspace; undefined above root. */
function resolve(path: string): string | undefined {
  const out: string[] = [];
  for (const part of (path.startsWith("/") ? path : `/workspace/${path}`).split(
    "/",
  )) {
    if (part === "" || part === ".") continue;
    if (part !== "..") out.push(part);
    else if (out.pop() === undefined) return undefined;
  }
  return `/${out.join("/")}`;
}

async function* chunks(bytes: Uint8Array): AsyncIterable<Uint8Array> {
  for (let at = 0; at < bytes.length; at += CHUNK)
    yield bytes.subarray(at, at + CHUNK);
}

export function fakeSandbox(script: unknown = {}): FakeSandbox {
  const { tools = {}, snapshots: scripted = {} } = SandboxScript.parse(script);
  const live = new Map<string, SandboxSession>();
  const captured = new Map<string, Captured>();
  const operations = new Map<string, Operation>();
  let creates = 0;
  let serial = 0;

  const failFile = (code: FileFailure["code"], path: string) =>
    err({ code, message: `${code}: ${path}` });

  const session = (id: string, tree: Tree): SandboxSession => {
    const ran = new Map<string, string>();
    const self: SandboxSession = {
      id: SandboxId.parse(id),
      exec: async (command, options) => {
        const name = command[0] ?? "";
        const tool = tools[name];
        ran.set(options.processKey, name);
        const deduped = tool?.executed_keys?.[options.processKey];
        const out = tool === undefined ? "" : (deduped ?? tool.output);
        const error = tool === undefined ? `${name}: command not found\n` : "";
        const code = tool === undefined ? 127 : tool.is_error ? 1 : 0;
        return ok({
          exit_code: Promise.resolve(code),
          stdout: chunks(utf8.encode(out)),
          stderr: chunks(utf8.encode(error)),
        } satisfies ExecOutput);
      },
      terminate: async (processKey) => {
        const name = ran.get(processKey);
        if (name === undefined) return ok("unknown");
        const process = tools[name]?.process;
        if (process === undefined) return ok("already_exited");
        return ok(process === "terminated" ? "terminated" : "unknown");
      },
      upload: async (path, data) => {
        const at = resolve(path);
        if (at === undefined) return failFile("invalid_path", path);
        tree.set(at, data.slice());
        return ok(undefined);
      },
      download: async (path) => {
        const at = resolve(path);
        if (at === undefined) return failFile("invalid_path", path);
        const bytes = tree.get(at);
        if (bytes !== undefined) return ok(bytes.slice());
        const dir = tree.keys().some((p) => p.startsWith(`${at}/`));
        return failFile(dir ? "is_directory" : "not_found", path);
      },
      snapshot: async (operationKey) => {
        serial += 1;
        const data: SnapshotData = {
          snapshot_id: SnapshotId.parse(`snap_fake_${serial}`),
          provider: INFO.provider,
          sandbox_id: self.id,
          capture_class: "filesystem",
          expires_at: null,
          manifest_hash: manifestHash(tree),
          quiesced: { frozen: [], stopped: [], excluded: [] },
        };
        captured.set(data.snapshot_id, { tree: new Map(tree), data });
        operations.set(operationKey, {
          kind: "snapshot",
          id: data.snapshot_id,
        });
        return ok(data);
      },
      close: async () => {
        live.delete(id);
        return ok(undefined);
      },
    };
    live.set(id, self);
    return self;
  };

  const open = (
    operationKey: string,
    id: string,
    tree: Tree,
  ): SandboxSession => {
    creates += 1;
    operations.set(operationKey, { kind: "sandbox", id });
    return session(id, tree);
  };

  const restore: Sandbox["restore"] = async (snapshotId, operationKey) => {
    const script = scripted[snapshotId];
    if (script !== undefined) {
      const made = open(operationKey, script.restore_sandbox_id, new Map());
      if (script.create_lookup === "unsupported")
        operations.set(operationKey, { kind: "unsupported" });
      return script.restore_response === "lost"
        ? err({ code: "unavailable", message: "the create response was lost" })
        : ok(made);
    }
    const snap = captured.get(snapshotId);
    if (snap === undefined)
      return err({
        code: "snapshot_missing",
        message: `no snapshot ${snapshotId}`,
      });
    serial += 1;
    const tree = new Map(snap.tree);
    if (manifestHash(tree) !== snap.data.manifest_hash)
      return err({
        code: "snapshot_manifest_mismatch",
        message: `the restored tree of ${snapshotId} fails its manifest`,
      });
    return ok(open(operationKey, `sbx_fake_${serial}`, tree));
  };

  const find = <T>(
    operationKey: string,
    kind: "sandbox" | "snapshot",
    value: (id: string) => T | undefined,
  ): LookupResult<T> => {
    const op = operations.get(operationKey);
    if (op?.kind === "unsupported")
      return { status: "unknown", reason: "this create can't be looked up" };
    const found = op?.kind === kind ? value(op.id) : undefined;
    return found === undefined
      ? { status: "not_found" }
      : { status: "found", value: found };
  };

  return {
    info: INFO,
    creates: () => creates,
    create: async (operationKey) => {
      serial += 1;
      return ok(open(operationKey, `sbx_fake_${serial}`, new Map()));
    },
    restore,
    lookup: async (key) => find(key, "sandbox", (id) => live.get(id)),
    lookupSnapshot: async (key) =>
      find(key, "snapshot", (id) => captured.get(id)?.data),
    attach: async (ref) => {
      const found = live.get(ref);
      return found === undefined
        ? err({ code: "not_found", message: `no live sandbox ${ref}` })
        : ok(found);
    },
    release: async (ref): Promise<Result<"released" | "already_gone", never>> =>
      ok(captured.delete(ref) ? "released" : "already_gone"),
  };
}
