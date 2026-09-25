import { describe, expect, test } from "bun:test";
import {
  chmodSync,
  lstatSync,
  mkdirSync,
  mkdtempSync,
  readlinkSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { err, ok } from "../../../src/result";
import type { Trees } from "../../../src/sandbox/protocol";
import { joined } from "../../../src/sandbox/remote/bytes";
import {
  EXPORT_TREE_SCRIPT,
  execScript,
  importTreeScript,
  WORKSPACE,
} from "../../../src/sandbox/remote/scripts";
import { placeTree, treeHash } from "../../../src/sandbox/trees";
import { memoryArtifacts } from "../../../src/store/artifacts";
import { CTX } from "../context";
import { sampleTree } from "./trees";

// The kit's scripts run in a real `sh`, as a provider would run them, instead of being read
// back by the emulated machine.

const text = new TextDecoder();

function sh(script: string, env: Record<string, string>) {
  const proc = Bun.spawnSync(["/bin/sh", "-c", script], { env });
  return {
    exit: proc.exitCode,
    stdout: text.decode(proc.stdout),
    stderr: text.decode(proc.stderr),
  };
}

describe("exec in a real sh", () => {
  // `probe` sits outside the libc default /bin:/usr/bin, like python3 in /usr/local/bin on
  // python:* images; it is `env`, so it prints exactly the environment it got.
  const bin = mkdtempSync(join(tmpdir(), "threads-bin-"));
  symlinkSync("/usr/bin/env", join(bin, "probe"));
  const provider = { PATH: `${bin}:/usr/bin:/bin`, SECRET: "host-only" };
  const run = (command: string[], env: Record<string, string>) =>
    sh(
      execScript({
        command,
        cwd: tmpdir(),
        env,
        processKey: "k",
        stdin: false,
      }),
      provider,
    );

  test("argv[0] resolves on the provider PATH and runs with exactly the tool env", () => {
    expect(run(["probe"], {})).toEqual({ exit: 0, stdout: "", stderr: "" });
    expect(run(["probe"], { K: "v w" }).stdout).toBe("K=v w\n");
  });

  test("a tool PATH does not change where argv[0] is found", () => {
    expect(run(["probe"], { PATH: "/nowhere" }).stdout).toBe("PATH=/nowhere\n");
  });

  test("a shell builtin is not a command: only an executable file on PATH runs", () => {
    const builtin = run(["export"], {});
    expect(builtin.exit).toBe(127);
    expect(builtin.stderr).toContain("threads: command not found: export");
  });

  test("a missing command exits 127 and says so", () => {
    const missing = run(["no-such-threads-command"], {});
    expect(missing.exit).toBe(127);
    expect(missing.stderr).toContain(
      "threads: command not found: no-such-threads-command",
    );
  });
});

// A fixed tree: nested dirs, a non-ASCII name, a space, an executable and an empty file. Its
// hash was pinned by the retired in-sandbox manifest script (find, stat, sha256sum), and Python
// pins it too (tests/adapters/sandboxes/test_posix_sh.py): trees hash as manifests did.
const TREE: readonly (readonly [string, string, number])[] = [
  ["a.txt", "hello\n", 0o644],
  ["dir/sub/run.sh", "#!/bin/sh\necho hi\n", 0o755],
  ["dir/with space.md", "x", 0o600],
  ["héllo/ünï.txt", "unicode", 0o644],
  ["empty", "", 0o644],
];
const TREE_HASH =
  "5002ad0ef59bdacdec8326269f3818c29b9f57ec31ff8c1973451a54a5b2a60f";

/** The kit's tree scripts run by a real `sh` over `root` as /workspace (bsdtar on macOS). */
function shellTrees(root: string): Trees {
  const here = (script: string) => script.replaceAll(WORKSPACE, root);
  // macOS tar would add AppleDouble `._` entries for extended metadata.
  const env = { PATH: "/usr/bin:/bin", COPYFILE_DISABLE: "1" };
  return {
    exportTree: async () => {
      const proc = Bun.spawn(["/bin/sh", "-c", here(EXPORT_TREE_SCRIPT)], {
        env,
        stdout: "pipe",
        stderr: "pipe",
      });
      return ok({
        exit_code: proc.exited,
        stdout: proc.stdout,
        stderr: proc.stderr,
      });
    },
    importTree: async (tar) => {
      const path = join(mkdtempSync(join(tmpdir(), "threads-up-")), "t.tar");
      writeFileSync(path, await joined(tar));
      const proc = Bun.spawnSync(
        ["/bin/sh", "-c", here(importTreeScript(path))],
        {
          env,
        },
      );
      if (lstatSync(path, { throwIfNoEntry: false }) !== undefined)
        throw new Error("the import left its archive behind");
      return proc.exitCode === 0
        ? ok(undefined)
        : err({ code: "unavailable", message: text.decode(proc.stderr) });
    },
  };
}

describe("the tree scripts in a real sh", () => {
  test("an export hashes a tree as the retired manifest script did", async () => {
    const root = mkdtempSync(join(tmpdir(), "threads-tree-"));
    for (const [path, body, mode] of TREE) {
      mkdirSync(join(root, path, ".."), { recursive: true });
      writeFileSync(join(root, path), body);
      chmodSync(join(root, path), mode);
    }
    expect(await treeHash(shellTrees(root), CTX)).toEqual(ok(TREE_HASH));
  });

  test("placeTree keeps modes and symlinks, and masks setuid to 0755", async () => {
    const artifacts = memoryArtifacts();
    const tree = await sampleTree(artifacts);
    const root = mkdtempSync(join(tmpdir(), "threads-place-"));
    expect(await placeTree(shellTrees(root), tree, artifacts, CTX)).toEqual(
      ok(undefined),
    );
    expect(lstatSync(join(root, "bin/run")).mode & 0o7777).toBe(0o755);
    expect(lstatSync(join(root, "bin/su")).mode & 0o7777).toBe(0o755);
    expect(readlinkSync(join(root, "link"))).toBe("bin/run");
  });

  test("placeTree refuses a /workspace that isn't empty", async () => {
    const root = mkdtempSync(join(tmpdir(), "threads-full-"));
    writeFileSync(join(root, "left"), "x");
    const placed = await placeTree(
      shellTrees(root),
      { tree_version: 1, entries: [] },
      memoryArtifacts(),
      CTX,
    );
    expect(placed.ok ? undefined : placed.error.code).toBe(
      "workspace_not_empty",
    );
  });
});
