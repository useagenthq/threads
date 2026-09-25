import { describe, expect, test } from "bun:test";
import {
  execute,
  fakeSandbox,
  manifestHash,
  manifestOf,
  type Sandbox,
  type SandboxSession,
} from "../../src/sandbox";
import { memoryArtifacts } from "../../src/store";
import { unwrap } from "../store/helpers";
import { CTX, changingInputs, type MutableCall } from "./context";

const bytes = (text: string): Uint8Array => new TextEncoder().encode(text);
const text = (b: Uint8Array): string => new TextDecoder().decode(b);
const EMPTY = manifestHash([]);

async function lookup(sandbox: Sandbox, key: string) {
  const answer = await sandbox.lookup?.(key, CTX);
  if (answer === undefined) throw new Error("the fake looks creates up");
  return answer;
}

async function session(script: unknown = {}): Promise<SandboxSession> {
  return unwrap(await fakeSandbox(script).create("op-1", CTX));
}

describe("fakeSandbox files and snapshots", () => {
  test("a restored snapshot has the file; a write in the child is invisible to the parent", async () => {
    const sandbox = fakeSandbox();
    const parent = unwrap(await sandbox.create("op-parent", CTX));
    unwrap(await parent.upload("notes.txt", bytes("v1"), CTX));
    const snap = unwrap(await parent.snapshot("op-snap", CTX));
    expect(snap.manifest_hash).toBe(
      manifestHash(
        manifestOf(new Map([["/workspace/notes.txt", bytes("v1")]])),
      ),
    );

    const child = unwrap(
      await sandbox.restore(
        snap.snapshot_id,
        snap.manifest_hash,
        "op-child",
        CTX,
      ),
    );
    expect(child.id).not.toBe(parent.id);
    expect(
      text(unwrap(await child.download("/workspace/notes.txt", CTX))),
    ).toBe("v1");
    unwrap(await child.upload("notes.txt", bytes("v2"), CTX));
    expect(text(unwrap(await parent.download("notes.txt", CTX)))).toBe("v1");
    expect(sandbox.creates()).toBe(2);
  });

  test("a restore whose tree fails the snapshot's hash is refused and released", async () => {
    const sandbox = fakeSandbox({
      snapshots: {
        snap_01: { restore_sandbox_id: "sbx_child_01", manifest: [] },
      },
    });
    const bad = await sandbox.restore("snap_01", "0".repeat(64), "op-a", CTX);
    expect(bad.ok ? "ok" : bad.error.code).toBe("snapshot_manifest_mismatch");
    expect(sandbox.creates()).toBe(1);
    expect((await sandbox.attach("sbx_child_01", CTX)).ok).toBe(false);
    const good = await sandbox.restore("snap_01", EMPTY, "op-b", CTX);
    expect(good.ok).toBe(true);
  });

  test("paths are typed failures, never throws", async () => {
    const s = await session();
    unwrap(await s.upload("dir/a", bytes("a"), CTX));
    const codes = await Promise.all(
      ["../../../etc/passwd", "missing", "dir"].map(async (p) => {
        const r = await s.download(p, CTX);
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
          manifest: [],
          restore_response: "lost",
          create_lookup: "found",
        },
      },
    });
    const lost = await sandbox.restore("snap_01", EMPTY, "op-a", CTX);
    expect(lost.ok ? "ok" : lost.error.code).toBe("unavailable");
    const found = unwrap(await lookup(sandbox, "op-a"));
    expect(found.status === "found" ? found.value.id : "none").toBe(
      "sbx_child_01",
    );
    expect(unwrap(await lookup(sandbox, "op-never"))).toEqual({
      status: "not_found",
    });
    expect(sandbox.creates()).toBe(1);
  });

  test("a create the adapter can't look up answers unknown", async () => {
    const sandbox = fakeSandbox({
      snapshots: {
        snap_01: {
          restore_sandbox_id: "sbx_child_01",
          manifest: [],
          restore_response: "lost",
          create_lookup: "unsupported",
        },
      },
    });
    await sandbox.restore("snap_01", EMPTY, "op-a", CTX);
    const answer = unwrap(await lookup(sandbox, "op-a"));
    expect(answer.status).toBe("unknown");
  });

  test("a missing snapshot is snapshot_missing; release is released, then already_gone", async () => {
    const sandbox = fakeSandbox();
    const missing = await sandbox.restore("snap_nope", EMPTY, "op-x", CTX);
    expect(missing.ok ? "ok" : missing.error.code).toBe("snapshot_missing");
    const s = unwrap(await sandbox.create("op-1", CTX));
    const snap = unwrap(await s.snapshot("op-snap", CTX));
    expect(await sandbox.lookupSnapshot?.("op-snap", CTX)).toEqual({
      ok: true,
      value: { status: "found", value: snap },
    });
    expect(unwrap(await sandbox.release(snap.snapshot_id, CTX))).toBe(
      "released",
    );
    expect(unwrap(await sandbox.release(snap.snapshot_id, CTX))).toBe(
      "already_gone",
    );
  });

  test("attach finds a live sandbox; a closed one is not_found", async () => {
    const sandbox = fakeSandbox();
    const s = unwrap(await sandbox.create("op-1", CTX));
    expect(unwrap(await sandbox.attach(s.id, CTX)).id).toBe(s.id);
    unwrap(await s.close(CTX));
    const gone = await sandbox.attach(s.id, CTX);
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
      execute(s, ["deploy"], CTX, { processKey: key }, artifacts);
    expect(unwrap(await run("b:call_1")).stdout).toBe("deduped");
    expect(unwrap(await run("b:call_2"))).toEqual({
      exit_code: 0,
      stdout: "deployed",
      stderr: "",
      truncated: false,
    });
    expect(unwrap(await s.terminate("b:call_2", CTX))).toBe("terminated");
    expect(unwrap(await s.terminate("b:never", CTX))).toBe("unknown");
  });

  test("an unscripted command exits 127 with its error on stderr", async () => {
    const s = await session(script);
    const r = unwrap(
      await execute(s, ["ls"], CTX, { processKey: "k" }, memoryArtifacts()),
    );
    expect([r.exit_code, r.stdout, r.stderr]).toEqual([
      127,
      "",
      "ls: command not found\n",
    ]);
  });

  test("a caller bug throws: empty command, bad or reserved env name", async () => {
    const s = await session(script);
    await expect(s.exec([], CTX, { processKey: "k" })).rejects.toThrow(
      "exec needs a command",
    );
    for (const [name, error] of [
      ["BAD NAME", "not a shell name"],
      ["__t_keep", "reserved"],
    ] as const)
      await expect(
        s.exec(["ls"], CTX, { processKey: "k", env: { [name]: "x" } }),
      ).rejects.toThrow(error);
  });

  test("exec runs what it checked, even if the caller changes it during the fence", async () => {
    const s = await session(script);
    const call: MutableCall = {
      command: ["deploy"],
      options: { processKey: "b:held", cwd: "/workspace", env: { A: "1" } },
    };
    const ctx = changingInputs(call);
    const ran = unwrap(
      await execute(s, call.command, ctx, call.options, memoryArtifacts()),
    );
    expect(ran.stdout).toBe("deployed");
    expect(unwrap(await s.terminate("b:held", CTX))).toBe("terminated");
  });

  test("large output keeps head and tail previews and spills the whole to an artifact", async () => {
    const big = `${"a".repeat(10_000)}MIDDLE${"z".repeat(10_000)}`;
    const s = await session({ tools: { dump: { output: big } } });
    const artifacts = memoryArtifacts();
    const r = unwrap(
      await execute(s, ["dump"], CTX, { processKey: "k" }, artifacts, 100),
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
    expect(text(unwrap(await artifacts.get(ref.sha256)))).toBe(big);
  });

  test("a script with an unknown key is refused", () => {
    expect(() => fakeSandbox({ tools: {}, extra: true })).toThrow();
  });
});

describe("the fake's POSIX commands the built-in file tools use", () => {
  const run = async (s: SandboxSession, command: readonly string[]) =>
    unwrap(
      await execute(s, command, CTX, { processKey: "k" }, memoryArtifacts()),
    );

  test("find lists files; grep -rnIE prints path:line:text and exits 1 on no match", async () => {
    const s = await session();
    unwrap(
      await s.upload("src/a.ts", bytes("const a = 1;\nexport {a};\n"), CTX),
    );
    unwrap(await s.upload("src/b.md", bytes("# b\n"), CTX));
    unwrap(await s.upload("top.txt", bytes("a = 2\n"), CTX));
    const found = await run(s, ["find", "/workspace/src", "-type", "f"]);
    expect(found.stdout).toBe("/workspace/src/a.ts\n/workspace/src/b.md\n");
    const grep = ["grep", "-rnIE", "-e"];
    const hit = await run(s, [...grep, "a = [0-9]", "--", "/workspace"]);
    expect([hit.exit_code, hit.stdout]).toEqual([
      0,
      "/workspace/src/a.ts:1:const a = 1;\n/workspace/top.txt:1:a = 2\n",
    ]);
    const none = await run(s, [...grep, "zzz", "--", "/workspace"]);
    expect(none.exit_code).toBe(1);
  });

  test("sh -c runs the scripted tool its command names; exec sees exactly the env given", async () => {
    const sandbox = fakeSandbox({ tools: { df: { output: "/ 40%" } } });
    const s = unwrap(await sandbox.create("op-1", CTX));
    const r = unwrap(
      await execute(
        s,
        ["sh", "-c", "df -h"],
        CTX,
        { processKey: "k", env: {} },
        memoryArtifacts(),
      ),
    );
    expect(r.stdout).toBe("/ 40%");
    expect(sandbox.execs()).toEqual([
      { command: ["sh", "-c", "df -h"], env: {} },
    ]);
  });
});
