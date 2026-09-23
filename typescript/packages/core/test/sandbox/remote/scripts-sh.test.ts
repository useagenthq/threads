import { describe, expect, test } from "bun:test";
import {
  chmodSync,
  mkdirSync,
  mkdtempSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { sha256Hex } from "../../../src/hash";
import { manifestHash } from "../../../src/sandbox";
import {
  execScript,
  MANIFEST_SCRIPT,
  parseManifest,
  quote,
  WORKSPACE,
} from "../../../src/sandbox/remote/scripts";

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

  test("a missing command exits 127 and says so", () => {
    const missing = run(["no-such-threads-command"], {});
    expect(missing.exit).toBe(127);
    expect(missing.stderr).toContain(
      "threads: command not found: no-such-threads-command",
    );
  });
});

// A fixed tree: nested dirs, a non-ASCII name, a space, an executable and an empty file. Python
// pins the same hash (tests/adapters/sandboxes/test_posix_linux.py), so both languages agree.
const TREE: readonly (readonly [string, string, number])[] = [
  ["a.txt", "hello\n", 0o644],
  ["dir/sub/run.sh", "#!/bin/sh\necho hi\n", 0o755],
  ["dir/with space.md", "x", 0o600],
  ["héllo/ünï.txt", "unicode", 0o644],
  ["empty", "", 0o644],
];
const TREE_HASH =
  "5002ad0ef59bdacdec8326269f3818c29b9f57ec31ff8c1973451a54a5b2a60f";

describe.skipIf(process.platform !== "linux")(
  "the manifest in a real sh",
  () => {
    test("lists the tree exactly as the host sees it", () => {
      const root = mkdtempSync(join(tmpdir(), "threads-manifest-"));
      const utf8 = new TextEncoder();
      for (const [path, body, mode] of TREE) {
        mkdirSync(join(root, path, ".."), { recursive: true });
        writeFileSync(join(root, path), body);
        chmodSync(join(root, path), mode);
      }
      const script = MANIFEST_SCRIPT.replace(
        `cd ${WORKSPACE}`,
        `cd ${quote(root)}`,
      );
      const out = Bun.spawnSync(["/bin/sh", "-c", script]);
      expect(out.exitCode).toBe(0);
      const manifest = parseManifest(out.stdout);
      expect(manifest).toEqual(
        TREE.map(([path, body, mode]) => ({
          path,
          mode,
          size: utf8.encode(body).length,
          sha256: sha256Hex(utf8.encode(body)),
        })).toSorted((a, b) => (a.path < b.path ? -1 : 1)),
      );
      expect(manifestHash(manifest ?? [])).toBe(TREE_HASH);
    });
  },
);

test("a manifest line with an empty mode (BSD stat) is refused", () => {
  const line = `\t1\t${"a".repeat(64)}\t61\n`;
  expect(parseManifest(new TextEncoder().encode(line))).toBeUndefined();
});
