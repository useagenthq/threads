import { describe, expect, test } from "bun:test";
import {
  chmodSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readdirSync,
  realpathSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { ConfigError } from "../../../src/agent/errors";
import { err, ok } from "../../../src/result";
import {
  devSandbox,
  type Sandbox,
  type SandboxContext,
  type SandboxSession,
} from "../../../src/sandbox";
import { confinement, DEFAULT_TOOL } from "../../../src/sandbox/dev/confine";
import type { Trees } from "../../../src/sandbox/protocol";
import type { Tree } from "../../../src/sandbox/tree/tree";
import { hashOnly, readTree } from "../../../src/sandbox/trees";
import { memoryArtifacts } from "../../../src/store/artifacts";
import { code, unwrap } from "../../store/helpers";
import { CTX } from "../context";

// devSandbox(): what it declares, the directory one operation key owns, and the host file
// operations that never follow a symlink. Running a command is confine.test.ts.

const STALE: SandboxContext = {
  ...CTX,
  fence: async () =>
    err({ code: "stale_epoch", message: "another owner took the branch" }),
};

/**
 * Stands in for the confinement in this file. These cases exercise the ledger, the host path walk
 * and the tree, never a confined command, so a trivial program that the probe accepts lets them
 * run on any host. The confinement itself is confine.test.ts, which skips loudly.
 */
const STUB = "/usr/bin/true";

const bytes = (text: string): Uint8Array => new TextEncoder().encode(text);

function root(): string {
  return mkdtempSync(join(tmpdir(), "threads-dev-test-"));
}

/** The session's Trees; every dev session has them. */
function trees(session: SandboxSession): Trees {
  const { exportTree, importTree } = session;
  if (exportTree === undefined || importTree === undefined)
    throw new Error("a dev session implements Trees");
  return { exportTree, importTree };
}

function looked(sandbox: Sandbox): NonNullable<Sandbox["lookup"]> {
  const { lookup } = sandbox;
  if (lookup === undefined) throw new Error("a final lookup is declared");
  return lookup;
}

const shape = (tree: Tree): readonly (readonly string[])[] =>
  tree.entries.map((e) => [e.path, e.kind]);

describe("devSandbox declarations", () => {
  test("no snapshots, a final create lookup, unconfirmed termination, egress enforced", () => {
    expect(devSandbox().info).toEqual({
      provider: "dev",
      egress: "enforced",
      capture_classes: [],
      browser: "none",
      desktop: "none",
      lookup: { create: "final", snapshot: "none" },
      termination: "unconfirmed",
    });
  });

  test("allowInternet declares egress unenforced", () => {
    expect(devSandbox({ allowInternet: true, tool: STUB }).info.egress).toBe(
      "unenforced",
    );
  });

  test("the confinement program defaults to the platform's, and one that can't run is capability_missing", async () => {
    const expected = DEFAULT_TOOL[process.platform];
    if (expected === undefined)
      throw new Error(`no confinement on ${process.platform}`);
    const made = confinement(false, undefined);
    expect(made.ok && made.value.tool).toBe(expected);

    const sandbox = devSandbox({
      root: root(),
      tool: "threads-no-such-confinement",
    });
    const failed = await sandbox.setup?.().then(
      () => undefined,
      (e: unknown) => e,
    );
    if (!(failed instanceof ConfigError))
      throw new Error(`expected a ConfigError, got ${String(failed)}`);
    expect(failed.code).toBe("capability_missing");
    expect(failed.message).toContain("threads-no-such-confinement");
  });

  test("the sandbox directories live in threads-dev in the host's temp directory", async () => {
    // /usr/bin/true stands in for the confinement, so this runs wherever the tests do.
    const sandbox = devSandbox({ tool: STUB });
    const session = unwrap(await sandbox.create("op-default-root", CTX));
    try {
      expect(
        existsSync(join(realpathSync(tmpdir()), "threads-dev", session.id)),
      ).toBe(true);
    } finally {
      await session.close(CTX);
    }
  });

  test("restore and release refuse: the dev sandbox has no snapshots", async () => {
    const sandbox = devSandbox({ root: root(), tool: STUB });
    expect(await sandbox.restore("snap", "hash", "op", CTX)).toEqual(
      err({
        code: "snapshot_missing",
        message: "the dev sandbox has no snapshots",
      }),
    );
    expect(code(await sandbox.release("snap", CTX))).toBe("unavailable");
  });

  test("a snapshot is unavailable, and a termination is never confirmed", async () => {
    const sandbox = devSandbox({ root: root(), tool: STUB });
    const session = unwrap(await sandbox.create("op-1", CTX));
    expect(code(await session.snapshot("op-snap", CTX))).toBe("unavailable");
    // Unconfirmed: an in-doubt call parks instead of being retried.
    expect(await session.terminate("key", CTX)).toEqual(ok("unknown"));
  });
});

describe("the directory an operation key owns", () => {
  test("create makes threads-dev-<key>, and lookup is final on it", async () => {
    const at = root();
    const sandbox = devSandbox({ root: at, tool: STUB });
    const session = unwrap(await sandbox.create("op-1", CTX));
    expect(session.id).toMatch(/^threads-dev-[0-9a-f]{32}$/);
    expect(existsSync(join(at, session.id))).toBe(true);

    const found = unwrap(await looked(sandbox)("op-1", CTX));
    expect(found.status).toBe("found");
    // Nothing else can have made that directory, so absence is proof.
    expect(unwrap(await looked(sandbox)("op-2", CTX))).toEqual({
      status: "not_found",
    });
  });

  test("a second create of the same key answers the same directory", async () => {
    const at = root();
    const sandbox = devSandbox({ root: at, tool: STUB });
    const first = unwrap(await sandbox.create("op-1", CTX));
    const again = unwrap(await sandbox.create("op-1", CTX));
    expect(again.id).toBe(first.id);
    expect(readdirSync(at)).toHaveLength(1);
  });

  test("attach finds a live sandbox, and refuses a ref that isn't one", async () => {
    const at = root();
    const sandbox = devSandbox({ root: at, tool: STUB });
    const session = unwrap(await sandbox.create("op-1", CTX));
    expect(unwrap(await sandbox.attach(session.id, CTX)).id).toBe(session.id);
    expect(code(await sandbox.attach("../escape", CTX))).toBe(
      "resource_unknown",
    );
    expect(code(await sandbox.attach("threads-dev-gone", CTX))).toBe(
      "not_found",
    );
  });

  test("close removes the directory", async () => {
    const at = root();
    const sandbox = devSandbox({ root: at, tool: STUB });
    const session = unwrap(await sandbox.create("op-1", CTX));
    expect(await session.close(CTX)).toEqual(ok(undefined));
    expect(readdirSync(at)).toEqual([]);
  });
});

describe("a stale owner touches nothing", () => {
  test("create, upload and download are refused before any syscall", async () => {
    const at = root();
    const sandbox = devSandbox({ root: at, tool: STUB });
    expect(code(await sandbox.create("op-1", STALE))).toBe("stale_epoch");
    expect(readdirSync(at)).toEqual([]);

    const session = unwrap(await sandbox.create("op-1", CTX));
    expect(
      code(await session.upload("/workspace/a.txt", bytes("x"), STALE)),
    ).toBe("stale_epoch");
    expect(readdirSync(join(at, session.id))).toEqual([]);
    expect(code(await session.download("/workspace/a.txt", STALE))).toBe(
      "stale_epoch",
    );
  });

  test("a stale writer spawns nothing", async () => {
    const at = root();
    // /usr/bin/true stands in for the confinement: the fence is refused before any spawn, so
    // this runs wherever the tests do and nothing is ever confined.
    const sandbox = devSandbox({ root: at, tool: STUB });
    const session = unwrap(await sandbox.create("op-1", CTX));
    const spawned = await session.exec(
      ["/bin/sh", "-c", "echo x > spawned.txt"],
      STALE,
      { processKey: "k-stale" },
    );
    expect(code(spawned)).toBe("stale_epoch");
    expect(readdirSync(join(at, session.id))).toEqual([]);
  });
});

describe("a symlink is never followed on the host", () => {
  test("read, write and export of c/x never reach a file outside the root", async () => {
    const at = root();
    writeFileSync(join(at, "outside.txt"), "a credential");
    const sandbox = devSandbox({ root: at, tool: STUB });
    const session = unwrap(await sandbox.create("op-1", CTX));
    const dir = join(at, session.id);
    // The rev-7 chain: `a/b -> ..` then `c -> a/b/..` leaves the root one lexical step at a
    // time, so a per-component check is the only thing that catches it.
    mkdirSync(join(dir, "a"));
    symlinkSync("..", join(dir, "a", "b"));
    symlinkSync("a/b/..", join(dir, "c"));

    expect(code(await session.download("/workspace/c/outside.txt", CTX))).toBe(
      "invalid_path",
    );
    expect(
      code(await session.upload("/workspace/c/planted.txt", bytes("x"), CTX)),
    ).toBe("invalid_path");
    expect(existsSync(join(at, "planted.txt"))).toBe(false);

    const tree = unwrap(await readTree(trees(session), CTX, hashOnly));
    expect(shape(tree)).toEqual([
      ["a", "dir"],
      ["a/b", "symlink"],
      ["c", "symlink"],
    ]);
  });

  test("a cwd reached through a symlink is refused, so no command starts there", async () => {
    const at = root();
    const sandbox = devSandbox({ root: at, tool: STUB });
    const session = unwrap(await sandbox.create("op-1", CTX));
    symlinkSync("..", join(at, session.id, "up"));
    const ran = await session.exec(["/bin/sh", "-c", "pwd"], CTX, {
      processKey: "k-cwd",
      cwd: "/workspace/up",
    });
    expect(code(ran)).toBe("invalid_path");
  });

  test("a path outside /workspace is refused: the dev sandbox holds only /workspace", async () => {
    const sandbox = devSandbox({ root: root(), tool: STUB });
    const session = unwrap(await sandbox.create("op-1", CTX));
    expect(code(await session.download("/etc/passwd", CTX))).toBe(
      "invalid_path",
    );
    expect(code(await session.upload("/tmp/planted", bytes("x"), CTX))).toBe(
      "invalid_path",
    );
  });
});

describe("the Trees capability on the directory", () => {
  test("a file, its mode and a symlink survive an export and an import", async () => {
    const at = root();
    const sandbox = devSandbox({ root: at, tool: STUB });
    const session = unwrap(await sandbox.create("op-1", CTX));
    const dir = join(at, session.id);
    mkdirSync(join(dir, "sub"));
    writeFileSync(join(dir, "sub", "run.sh"), "#!/bin/sh\n", { mode: 0o755 });
    symlinkSync("sub/run.sh", join(dir, "link"));

    const exported = unwrap(
      await readTree(trees(session), CTX, memoryArtifacts().sink),
    );
    expect(shape(exported)).toEqual([
      ["link", "symlink"],
      ["sub", "dir"],
      ["sub/run.sh", "file"],
    ]);

    const other = unwrap(await sandbox.create("op-2", CTX));
    const archive = unwrap(await trees(session).exportTree(CTX));
    expect(await trees(other).importTree(archive.stdout, CTX)).toEqual(
      ok(undefined),
    );
    const back = unwrap(await readTree(trees(other), CTX, hashOnly));
    expect(shape(back)).toEqual(shape(exported));
    const file = back.entries.find((e) => e.path === "sub/run.sh");
    expect(file?.kind === "file" && file.mode).toBe(0o755);
  });

  test("a setuid bit never survives an import", async () => {
    const at = root();
    const sandbox = devSandbox({ root: at, tool: STUB });
    const session = unwrap(await sandbox.create("op-1", CTX));
    const suid = join(at, session.id, "suid");
    writeFileSync(suid, "x");
    chmodSync(suid, 0o4755);

    const other = unwrap(await sandbox.create("op-2", CTX));
    const archive = unwrap(await trees(session).exportTree(CTX));
    expect(await trees(other).importTree(archive.stdout, CTX)).toEqual(
      ok(undefined),
    );
    const back = unwrap(await readTree(trees(other), CTX, hashOnly));
    const file = back.entries.find((e) => e.path === "suid");
    expect(file?.kind === "file" && file.mode).toBe(0o755);
  });
});
