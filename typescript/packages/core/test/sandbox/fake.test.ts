import { describe, expect, test } from "bun:test";
import {
  execute,
  fakeSandbox,
  manifestHash,
  type SandboxSession,
} from "../../src/sandbox";
import { memoryArtifacts } from "../../src/store";
import { unwrap } from "../store/helpers";

const bytes = (text: string): Uint8Array => new TextEncoder().encode(text);
const text = (b: Uint8Array): string => new TextDecoder().decode(b);

async function session(script: unknown = {}): Promise<SandboxSession> {
  return unwrap(await fakeSandbox(script).create("op-1"));
}

describe("fakeSandbox files and snapshots", () => {
  test("a restored snapshot has the file; a write in the child is invisible to the parent", async () => {
    const sandbox = fakeSandbox();
    const parent = unwrap(await sandbox.create("op-parent"));
    unwrap(await parent.upload("notes.txt", bytes("v1")));
    const snap = unwrap(await parent.snapshot("op-snap"));
    expect(snap.manifest_hash).toBe(
      manifestHash(new Map([["/workspace/notes.txt", bytes("v1")]])),
    );

    const child = unwrap(await sandbox.restore(snap.snapshot_id, "op-child"));
    expect(child.id).not.toBe(parent.id);
    expect(text(unwrap(await child.download("/workspace/notes.txt")))).toBe(
      "v1",
    );
    unwrap(await child.upload("notes.txt", bytes("v2")));
    expect(text(unwrap(await parent.download("notes.txt")))).toBe("v1");
    expect(sandbox.creates()).toBe(2);
  });

  test("paths are typed failures, never throws", async () => {
    const s = await session();
    unwrap(await s.upload("dir/a", bytes("a")));
    const codes = await Promise.all(
      ["../../../etc/passwd", "missing", "dir"].map(async (p) => {
        const r = await s.download(p);
        return r.ok ? "ok" : r.error.code;
      }),
    );
    expect(codes).toEqual(["invalid_path", "not_found", "is_directory"]);
  });

  test("lookup by operation key finds what a lost response created", async () => {
    const sandbox = fakeSandbox({
      snapshots: {
        snap_01: {
          restore_sandbox_id: "sbx_child_01",
          restore_response: "lost",
          create_lookup: "found",
        },
      },
    });
    const lost = await sandbox.restore("snap_01", "op-a");
    expect(lost.ok ? "ok" : lost.error.code).toBe("unavailable");
    const found = await sandbox.lookup?.("op-a");
    expect(found?.status === "found" ? found.value.id : "none").toBe(
      "sbx_child_01",
    );
    expect(await sandbox.lookup?.("op-never")).toEqual({ status: "not_found" });
    expect(sandbox.creates()).toBe(1);
  });

  test("a create the adapter can't look up answers unknown", async () => {
    const sandbox = fakeSandbox({
      snapshots: {
        snap_01: {
          restore_sandbox_id: "sbx_child_01",
          restore_response: "lost",
          create_lookup: "unsupported",
        },
      },
    });
    await sandbox.restore("snap_01", "op-a");
    const answer = await sandbox.lookup?.("op-a");
    expect(answer?.status).toBe("unknown");
  });

  test("a missing snapshot is snapshot_missing; release is released, then already_gone", async () => {
    const sandbox = fakeSandbox();
    const missing = await sandbox.restore("snap_nope", "op-x");
    expect(missing.ok ? "ok" : missing.error.code).toBe("snapshot_missing");
    const s = unwrap(await sandbox.create("op-1"));
    const snap = unwrap(await s.snapshot("op-snap"));
    expect(await sandbox.lookupSnapshot?.("op-snap")).toEqual({
      status: "found",
      value: snap,
    });
    expect(unwrap(await sandbox.release(snap.snapshot_id))).toBe("released");
    expect(unwrap(await sandbox.release(snap.snapshot_id))).toBe(
      "already_gone",
    );
  });

  test("attach finds a live sandbox; a closed one is not_found", async () => {
    const sandbox = fakeSandbox();
    const s = unwrap(await sandbox.create("op-1"));
    expect(unwrap(await sandbox.attach(s.id)).id).toBe(s.id);
    unwrap(await s.close());
    const gone = await sandbox.attach(s.id);
    expect(gone.ok ? "ok" : gone.error.code).toBe("not_found");
  });
});

describe("scripted exec", () => {
  const script = {
    tools: {
      deploy: {
        output: "deployed",
        executed_keys: { "b:call_1": "deduped" },
        process: "terminated",
      },
    },
  };

  test("a known key is provider dedup; terminate answers per the script", async () => {
    const s = await session(script);
    const artifacts = memoryArtifacts();
    const run = (key: string) =>
      execute(s, ["deploy"], { processKey: key }, artifacts);
    expect(unwrap(await run("b:call_1")).stdout).toBe("deduped");
    expect(unwrap(await run("b:call_2"))).toEqual({
      exit_code: 0,
      stdout: "deployed",
      stderr: "",
      truncated: false,
    });
    expect(unwrap(await s.terminate("b:call_2"))).toBe("terminated");
    expect(unwrap(await s.terminate("b:never"))).toBe("unknown");
  });

  test("an unscripted command exits 127 with its error on stderr", async () => {
    const s = await session(script);
    const r = unwrap(
      await execute(s, ["ls"], { processKey: "k" }, memoryArtifacts()),
    );
    expect([r.exit_code, r.stdout, r.stderr]).toEqual([
      127,
      "",
      "ls: command not found\n",
    ]);
  });

  test("large output keeps head and tail previews and spills the whole to an artifact", async () => {
    const big = `${"a".repeat(10_000)}MIDDLE${"z".repeat(10_000)}`;
    const s = await session({ tools: { dump: { output: big } } });
    const artifacts = memoryArtifacts();
    const r = unwrap(
      await execute(s, ["dump"], { processKey: "k" }, artifacts, 100),
    );
    expect(r.truncated).toBe(true);
    expect(r.stdout.startsWith("a".repeat(100))).toBe(true);
    expect(r.stdout.endsWith("z".repeat(100))).toBe(true);
    expect(r.stdout).toContain("[... 19806 bytes omitted ...]");
    expect(r.stdout).not.toContain("MIDDLE");
    const ref = r.full_output;
    if (ref === undefined)
      throw new Error("a truncated result has full_output");
    expect(ref.bytes).toBe(big.length);
    expect(text(unwrap(artifacts.get(ref.sha256)))).toBe(big);
  });

  test("a script with an unknown key is refused", () => {
    expect(() => fakeSandbox({ tools: {}, extra: true })).toThrow();
  });
});
