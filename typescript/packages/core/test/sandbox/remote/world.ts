import { manifestHash, SandboxScript } from "../../../src/sandbox";
import { WORKSPACE } from "../../../src/sandbox/remote/scripts";
import { type FileRec, Machine, seeded } from "./machine";

// A provider's state behind a mocked transport: live sandboxes (emulated machines) found by
// their operation key, and snapshots. Seeded from a conformance SandboxScript, so the fork
// cases run against every adapter: a scripted snapshot restores into its named sandbox, a
// `lost` restore creates but loses the response, an `unsupported` lookup fails.

type Snap = {
  readonly files: ReadonlyMap<string, FileRec>;
  readonly restoreId?: string;
  readonly lost?: boolean;
  readonly unlookable?: boolean;
};

export class World {
  readonly machines: Map<string, Machine> = new Map();
  readonly snapshots: Map<string, Snap> = new Map();
  private readonly keys = new Map<string, string>();
  private readonly unlookable = new Set<string>();
  private serial = 0;
  /** Provider create and restore calls that reached the backend. */
  creates = 0;

  constructor(script: unknown = {}) {
    const snapshots = SandboxScript.parse(script).snapshots ?? {};
    for (const [id, s] of Object.entries(snapshots))
      this.snapshots.set(id, {
        files: seeded("", s.manifest).files,
        restoreId: s.restore_sandbox_id,
        lost: s.restore_response === "lost",
        unlookable: s.create_lookup === "unsupported",
      });
  }

  /** A create reaching the provider: `lost` means it happened but the caller never hears. */
  create(
    key: string,
    snapshot: string | undefined,
  ):
    | { readonly id: string; readonly lost: boolean }
    | { readonly missing: true } {
    const snap =
      snapshot === undefined ? undefined : this.snapshots.get(snapshot);
    if (snapshot !== undefined && snap === undefined) return { missing: true };
    this.creates += 1;
    this.serial += 1;
    const id = snap?.restoreId ?? `sbx_${this.serial}`;
    const machine = new Machine(id);
    for (const [path, file] of snap?.files ?? []) machine.files.set(path, file);
    this.machines.set(id, machine);
    this.keys.set(key, id);
    if (snap?.unlookable === true) this.unlookable.add(key);
    return { id, lost: snap?.lost === true };
  }

  /** A lookup by key: the live sandbox, undefined, or a failure the provider returns. */
  find(key: string): string | undefined | "error" {
    if (this.unlookable.has(key)) return "error";
    const id = this.keys.get(key);
    return id !== undefined && this.machines.has(id) ? id : undefined;
  }

  kill(id: string): boolean {
    return this.machines.delete(id);
  }

  snapshot(id: string, name: string): string {
    const machine = this.machine(id);
    this.snapshots.set(name, { files: new Map(machine.files) });
    return name;
  }

  /** The manifest hash of a snapshot's /workspace tree, as the kit computes it. */
  hashOf(ref: string): string {
    const files = this.snapshots.get(ref)?.files ?? new Map();
    const entries = [...files]
      .filter(([p]) => p.startsWith(`${WORKSPACE}/`))
      .map(([p, f]) => ({
        path: p.slice(WORKSPACE.length + 1),
        mode: f.mode,
        size: f.size,
        sha256: f.sha256,
      }))
      .toSorted((a, b) => (a.path < b.path ? -1 : a.path > b.path ? 1 : 0));
    return manifestHash(entries);
  }

  deleteSnapshot(ref: string): boolean {
    return this.snapshots.delete(ref);
  }

  machine(id: string): Machine {
    const machine = this.machines.get(id);
    if (machine === undefined) throw new Error(`no sandbox ${id}`);
    return machine;
  }
}
