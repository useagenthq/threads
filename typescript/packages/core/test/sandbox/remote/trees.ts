import { describe, expect, test } from "bun:test";
import type {
  Sandbox,
  SandboxSession,
  Trees,
} from "../../../src/sandbox/protocol";
import type { Tree } from "../../../src/sandbox/tree/tree";
import { placeTree } from "../../../src/sandbox/trees";
import {
  type ArtifactStore,
  memoryArtifacts,
} from "../../../src/store/artifacts";
import { CTX } from "../context";
import { created, staleContext } from "./kit";
import type { World } from "./world";

// The Trees capability of every adapter built on the remote kit (spec/api.json
// SandboxSession.exportTree, importTree), run over each adapter's mocked transport.

/** A tree with an executable, a setuid file and a symlink, its file bytes in `artifacts`. */
export async function sampleTree(
  artifacts: Pick<ArtifactStore, "put">,
): Promise<Tree> {
  const body = new TextEncoder().encode("#!/bin/sh\necho hi\n");
  const sha256 = await artifacts.put(body);
  const file = (path: string, mode: number) =>
    ({ path, kind: "file", mode, size: body.length, sha256 }) as const;
  return {
    tree_version: 1,
    entries: [
      { path: "bin", kind: "dir", mode: 0o755 },
      file("bin/run", 0o755),
      file("bin/su", 0o4755),
      { path: "link", kind: "symlink", target: "bin/run" },
    ],
  };
}

/** The session's Trees; fails the test when the adapter doesn't offer them. */
export function trees(session: SandboxSession): Trees {
  const { exportTree, importTree } = session;
  if (exportTree === undefined || importTree === undefined)
    throw new Error("the session offers Trees");
  return { exportTree, importTree };
}

export function treesSuite(
  name: string,
  make: () => { readonly sandbox: Sandbox; readonly world: World },
): void {
  describe(`sandbox trees: ${name}`, () => {
    test("placeTree keeps a 0755 file and a symlink, and masks setuid to 0755", async () => {
      const { sandbox, world } = make();
      const box = await created(sandbox);
      const artifacts = memoryArtifacts();
      const placed = await placeTree(
        trees(box),
        await sampleTree(artifacts),
        artifacts,
        CTX,
      );
      expect(placed).toEqual({ ok: true, value: undefined });
      const machine = world.machine(box.id);
      expect(machine.files.get("/workspace/bin/run")?.mode).toBe(0o755);
      expect(machine.files.get("/workspace/bin/su")?.mode).toBe(0o755);
      expect(machine.links.get("/workspace/link")).toBe("bin/run");
      // The uploaded archive is gone.
      expect(
        [...machine.files.keys()].filter((p) => p.startsWith("/tmp/")),
      ).toEqual([]);
    });

    test("placeTree refuses a /workspace that isn't empty", async () => {
      const { sandbox } = make();
      const box = await created(sandbox);
      await box.upload("/workspace/left", new Uint8Array([1]), CTX);
      const artifacts = memoryArtifacts();
      const placed = await placeTree(
        trees(box),
        await sampleTree(artifacts),
        artifacts,
        CTX,
      );
      expect(placed.ok ? undefined : placed.error).toEqual({
        code: "workspace_not_empty",
        message:
          "the sandbox's /workspace isn't empty (left); workspace inputs and restores need it empty",
      });
    });

    test("a stale context exports and imports nothing", async () => {
      const { sandbox, world } = make();
      const box = await created(sandbox);
      const machine = world.machine(box.id);
      const scripts = machine.scripts.length;
      const stale = staleContext();
      const exported = await trees(box).exportTree(stale);
      const imported = await trees(box).importTree(
        (async function* () {
          yield new Uint8Array(1024);
        })(),
        stale,
      );
      expect(
        [exported, imported].map((r) => (r.ok ? "ok" : r.error.code)),
      ).toEqual(["stale_epoch", "stale_epoch"]);
      expect(machine.scripts.length).toBe(scripts);
    });
  });
}
