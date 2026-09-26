import { describe, expect, test } from "bun:test";
import { fakeSandbox, ownerContext, type Sandbox } from "../../src/sandbox";
import type { Tree } from "../../src/sandbox/tree/tree";
import type { ArtifactStore } from "../../src/store/artifacts";
import { lazySession, type Placement } from "../../src/tools/session";
import { fixture, ROOT, THREAD, unwrap } from "../store/helpers";

// A thread with workspace inputs (spec/schema/README.md, Materialization): the pinned tree is
// in /workspace before the ledger row goes live, and a crash in that window is settled by the
// next session instead of leaking a sandbox.

async function treeOf(
  artifacts: ArtifactStore,
  files: Readonly<Record<string, string>>,
): Promise<Tree> {
  const utf8 = new TextEncoder();
  const entries = [];
  for (const [path, body] of Object.entries(files).toSorted()) {
    const bytes = utf8.encode(body);
    entries.push({
      path,
      kind: "file" as const,
      mode: 0o644,
      size: bytes.length,
      sha256: await artifacts.put(bytes),
    });
  }
  return { tree_version: 1, entries };
}

async function opened(sandbox: Sandbox) {
  const f = await fixture();
  unwrap(await f.store.createBranch(THREAD, ROOT));
  const writer = unwrap(await f.store.acquire(ROOT, "holder-a"));
  const tree = await treeOf(f.artifacts, { "NOTES.md": "# notes\n" });
  const placement: Placement = { tree, artifacts: f.artifacts };
  return { f, writer, tree, placement, sandbox };
}

const states = async (f: Awaited<ReturnType<typeof fixture>>) =>
  unwrap(await f.store.ledger.rows()).map((r) => r.state);

describe("workspace materialization", () => {
  test("the pinned tree is in /workspace, and the row is live", async () => {
    const sandbox = fakeSandbox();
    const { f, writer, placement } = await opened(sandbox);
    const session = unwrap(
      await lazySession(f.store.ledger, writer, sandbox, placement)(),
    );
    const got = unwrap(
      await session.download("/workspace/NOTES.md", ownerContext(writer)),
    );
    expect(new TextDecoder().decode(got)).toBe("# notes\n");
    expect(await states(f)).toEqual(["live"]);
  });

  test("a sandbox that can't take the tree is released, and the tool call gets the error", async () => {
    const fake = fakeSandbox();
    // importTree that drops everything: the re-export can't match the pinned tree.
    const sandbox: Sandbox = {
      ...fake,
      create: async (key, context) => {
        const made = await fake.create(key, context);
        if (!made.ok) return made;
        return {
          ...made,
          value: {
            ...made.value,
            importTree: async () => ({ ok: true as const, value: undefined }),
          },
        };
      },
    };
    const { f, writer, placement } = await opened(sandbox);
    const got = await lazySession(f.store.ledger, writer, sandbox, placement)();
    expect(got.ok).toBe(false);
    expect(got.ok ? "ok" : got.error.code).toBe("workspace_mismatch");
    expect(await states(f)).toEqual(["released"]);
  });

  test("a session without the Trees capability is capability_missing", async () => {
    const fake = fakeSandbox();
    const sandbox: Sandbox = {
      ...fake,
      create: async (key, context) => {
        const made = await fake.create(key, context);
        if (!made.ok) return made;
        const { exportTree: _e, importTree: _i, ...rest } = made.value;
        return { ...made, value: rest };
      },
    };
    const { f, writer, placement } = await opened(sandbox);
    const got = await lazySession(f.store.ledger, writer, sandbox, placement)();
    expect(got.ok ? "ok" : got.error.code).toBe("capability_missing");
    expect(await states(f)).toEqual(["released"]);
  });

  test("a crash between create and live is settled by the next session, not leaked", async () => {
    const sandbox = fakeSandbox();
    const { f, writer, placement } = await opened(sandbox);
    // The crash: a pending row whose sandbox exists, exactly where placeTree runs.
    const row = unwrap(await f.store.ledger.begin(writer, "sandbox", "fake"));
    unwrap(await sandbox.create(row.operation_key, ownerContext(writer)));
    unwrap(await lazySession(f.store.ledger, writer, sandbox, placement)());
    expect(await states(f)).toEqual(["released", "live"]);
    expect(sandbox.creates()).toBe(2);
  });

  test("a crashed capture-scratch row is settled too: the kinds share one begin", async () => {
    const sandbox = fakeSandbox();
    const { f, writer, placement } = await opened(sandbox);
    // capture.ts and fork.ts open their sandboxes with the same begin(…, "sandbox", …).
    const scratch = unwrap(
      await f.store.ledger.begin(writer, "sandbox", "fake"),
    );
    unwrap(await sandbox.create(scratch.operation_key, ownerContext(writer)));
    unwrap(await lazySession(f.store.ledger, writer, sandbox, placement)());
    const rows = unwrap(await f.store.ledger.rows());
    expect(rows.find((r) => r.resource_id === scratch.resource_id)?.state).toBe(
      "released",
    );
  });

  test("without a workspace nothing is placed, and the crash path still settles", async () => {
    const sandbox = fakeSandbox();
    const { f, writer } = await opened(sandbox);
    const row = unwrap(await f.store.ledger.begin(writer, "sandbox", "fake"));
    unwrap(await sandbox.create(row.operation_key, ownerContext(writer)));
    unwrap(await lazySession(f.store.ledger, writer, sandbox)());
    expect(await states(f)).toEqual(["released", "live"]);
  });
});
