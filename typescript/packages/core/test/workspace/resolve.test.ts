import { describe, expect, test } from "bun:test";
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { ConfigError } from "../../src/agent/errors";
import { register } from "../../src/redact/registry";
import type { Tree } from "../../src/sandbox/tree/tree";
import {
  type Resolved,
  resolveWorkspace,
  type Workspace,
} from "../../src/workspace/resolve";
import { dirOf, git, hasGit, link, repoOf } from "./fixture";

// agent({workspace}) resolved on the host (spec/schema/README.md, Workspace inputs): what the
// tree holds, what the pin says was skipped, and what is refused.

const NO_FORGE = {};
const keep = async (): Promise<undefined> => undefined;

const resolve = (ws: Workspace): Promise<Resolved> =>
  resolveWorkspace(ws, NO_FORGE, keep);

const paths = (tree: Tree): readonly string[] =>
  tree.entries.map((e) => e.path);

const skippedOf = (r: Resolved): readonly string[] =>
  r.pin.sources.flatMap((s) => (s.kind === "files" ? [] : s.skipped));

async function refuses(ws: Workspace, holds: string): Promise<string> {
  const failed = await resolve(ws).then(
    () => undefined,
    (e: unknown) => e,
  );
  expect(failed).toBeInstanceOf(ConfigError);
  const error = failed as ConfigError;
  expect(error.code).toBe("invalid_config");
  expect(error.message).toContain(holds);
  return error.message;
}

describe("files", () => {
  test("text and bytes, mode 0o644, no exclusions", async () => {
    const r = await resolve({
      files: { "NOTES.md": "# notes\n", ".env": "SECRET=1\n" },
    });
    expect(paths(r.tree)).toEqual([".env", "NOTES.md"]);
    expect(r.tree.entries.every((e) => e.kind === "file" && e.mode === 0o644));
    expect(r.pin.sources).toEqual([{ kind: "files" }]);
  });

  test("a path that isn't normal is refused", async () => {
    await refuses({ files: { "../out": "x" } }, "not a normal tree path");
  });
});

describe("localDir", () => {
  test("modes, symlinks and the path pinned as written", async () => {
    const root = dirOf({ "src/main.ts": "x", "bin/run*": "#!/bin/sh\n" });
    link(root, "bin/link", "../src/main.ts");
    const r = await resolveWorkspace({ localDir: root }, NO_FORGE, keep);
    expect(paths(r.tree)).toEqual([
      "bin",
      "bin/link",
      "bin/run",
      "src",
      "src/main.ts",
    ]);
    const run = r.tree.entries.find((e) => e.path === "bin/run");
    expect(run).toMatchObject({ kind: "file", mode: 0o755 });
    expect(r.tree.entries.find((e) => e.path === "bin/link")).toMatchObject({
      kind: "symlink",
      target: "../src/main.ts",
    });
    expect(r.pin.sources[0]).toMatchObject({ kind: "local_dir", path: root });
  });

  test("a symlink out of the directory is refused", async () => {
    const root = dirOf({ "a.ts": "x" });
    link(root, "escape", "../../etc/passwd");
    await refuses({ localDir: root }, "points outside it");
  });

  test("a directory that isn't readable is refused", async () => {
    await refuses(
      { localDir: "/no/such/workspace/dir" },
      "is not a readable directory",
    );
  });

  test("the deny-list keeps id_utils and id_token.ts, skips id_ed25519", async () => {
    const root = dirOf({
      "id_utils/index.ts": "x",
      "id_token.ts": "x",
      id_ed25519: "KEY",
      ".env": "A=1",
      ".npmrc": "//registry/:_authToken=t",
      "certs/server.pem": "---",
      "src/app.ts": "x",
    });
    const r = await resolveWorkspace({ localDir: root }, NO_FORGE, keep);
    expect(paths(r.tree)).toEqual([
      // certs/ stays: only the .pem inside it is deny-listed.
      "certs",
      "id_token.ts",
      "id_utils",
      "id_utils/index.ts",
      "src",
      "src/app.ts",
    ]);
    expect(skippedOf(r)).toEqual([
      ".env",
      ".npmrc",
      "certs/server.pem",
      "id_ed25519",
    ]);
  });

  test("more files than the limit is refused", async () => {
    const root = dirOf({ "a.ts": "x" });
    const files: Record<string, string> = {};
    for (let i = 0; i <= 10_000; i += 1) files[`f${i}.txt`] = "";
    mkdirSync(join(root, "many"), { recursive: true });
    for (const name of Object.keys(files))
      writeFileSync(join(root, "many", name), "");
    await refuses({ localDir: root }, "more than 10000 files after exclusions");
  });

  test("a file holding a registered secret is refused without the value", async () => {
    register("hunter2-hunter2-hunter2", "test");
    const root = dirOf({ "config.ts": "const t = 'hunter2-hunter2-hunter2';" });
    const message = await refuses(
      { localDir: root },
      "contains a secret value",
    );
    expect(message).not.toContain("hunter2");
  });

  test("two inputs naming one path is refused", async () => {
    const root = dirOf({ "NOTES.md": "from the directory" });
    await refuses(
      { localDir: root, files: { "NOTES.md": "from files" } },
      "two inputs give NOTES.md",
    );
  });
});

describe.skipIf(!hasGit)("a git work tree", () => {
  test(".git, gitignored paths and the deny-list never reach the tree", async () => {
    const root = repoOf({
      ".gitignore": "node_modules/\n.env.test\n",
      "node_modules/left-pad/index.js": "module.exports = 1",
      "node_modules/.env": "IN=modules",
      ".env": "A=1",
      ".env.test": "B=2",
      id_rsa: "KEY",
      ".npmrc": "//registry/:_authToken=t",
      "src/app.ts": "x",
    });
    writeFileSync(
      join(root, ".git", "config"),
      '[http]\n  extraheader = "Authorization: Basic c2VjcmV0"\n',
      { flag: "a" },
    );
    const r = await resolveWorkspace({ localDir: root }, NO_FORGE, keep);
    expect(paths(r.tree)).toEqual([".gitignore", "src", "src/app.ts"]);
    expect(skippedOf(r)).toEqual([
      ".env",
      ".env.test",
      ".git",
      ".npmrc",
      "id_rsa",
      "node_modules",
    ]);
    expect(JSON.stringify(r)).not.toContain("extraheader");
  });

  test("include re-admits one gitignored file", async () => {
    const root = repoOf({
      ".gitignore": ".env.test\nnode_modules/\n",
      ".env.test": "B=2",
      "node_modules/a.js": "1",
      "src/app.ts": "x",
    });
    const r = await resolveWorkspace(
      { localDir: root, include: [".env.test"] },
      NO_FORGE,
      keep,
    );
    expect(paths(r.tree)).toEqual([
      ".env.test",
      ".gitignore",
      "src",
      "src/app.ts",
    ]);
  });

  test("include on a directory re-admits its subtree, minus .git and deny-listed names", async () => {
    const root = repoOf({
      ".gitignore": "node_modules/\n",
      "node_modules/left-pad/index.js": "1",
      "node_modules/left-pad/.env": "A=1",
      "node_modules/.bin/tsc*": "#!/bin/sh\n",
      "node_modules/vendored/.git/config": "[core]\n",
      "src/app.ts": "x",
    });
    const r = await resolveWorkspace(
      { localDir: root, include: ["node_modules"] },
      NO_FORGE,
      keep,
    );
    expect(paths(r.tree)).toContain("node_modules/left-pad/index.js");
    expect(paths(r.tree)).toContain("node_modules/.bin/tsc");
    expect(paths(r.tree)).not.toContain("node_modules/left-pad/.env");
    expect(paths(r.tree).some((p) => p.includes(".git/"))).toBe(false);
  });

  test("an include that re-admits nothing is refused", async () => {
    const root = repoOf({ "src/app.ts": "x" });
    await refuses(
      { localDir: root, include: ["nothing.txt"] },
      "re-admits nothing",
    );
  });

  test("include can't name .git", async () => {
    const root = repoOf({ "src/app.ts": "x" });
    await refuses(
      { localDir: root, include: [".git/config"] },
      "a normal tree path outside .git",
    );
  });

  test("a submodule is refused, naming it", async () => {
    const root = repoOf({ "src/app.ts": "x" });
    const inner = repoOf({ "lib.ts": "x" });
    git(inner, "add", "-A");
    git(inner, "commit", "--quiet", "-m", "one");
    git(
      root,
      "-c",
      "protocol.file.allow=always",
      "submodule",
      "--quiet",
      "add",
      inner,
      "vendor",
    );
    await refuses({ localDir: root }, "submodules aren't copied");
  });

  test("a subdirectory of a repository honors its .gitignore", async () => {
    const root = repoOf({
      ".gitignore": "build/\n",
      "pkg/build/out.js": "1",
      "pkg/src/app.ts": "x",
    });
    const r = await resolveWorkspace(
      { localDir: join(root, "pkg") },
      NO_FORGE,
      keep,
    );
    expect(paths(r.tree)).toEqual(["src", "src/app.ts"]);
    expect(skippedOf(r)).toEqual(["build"]);
  });

  test("a work tree with no git on PATH is refused, naming the fix", async () => {
    const root = repoOf({ "src/app.ts": "x" });
    const path = process.env["PATH"];
    process.env["PATH"] = "/nonexistent";
    try {
      await refuses({ localDir: root }, "install git, or point localDir");
    } finally {
      process.env["PATH"] = path;
    }
  });
});
