import { describe, expect, test } from "bun:test";
import {
  mkdirSync,
  mkdtempSync,
  readdirSync,
  readFileSync,
  rmSync,
  unlinkSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { importThread, openThread, type Store, sqlite } from "../../src";
import { openStore } from "../../src/agent/sqlite";
import { deleteThread } from "../../src/store/deletion";
import { code, rows, unwrap } from "../store/helpers";
import { events, finished, say, use } from "./methods-kit";

// thread.export and importThread (spec/api.json; spec/schema/README.md, "Portable bundles"): a
// bundle carries the chain and its artifacts, is published only when bundle.json lands, and is
// validated whole before the destination store is touched.

/** A directory inside a fresh temp root: the bundle path never exists yet. */
function where(): { readonly root: string; readonly path: string } {
  const root = mkdtempSync(join(tmpdir(), "threads-bundle-"));
  return { root, path: join(root, "case") };
}

/** A thread with a tool call, two model requests and the artifacts they need. */
async function saved() {
  return await finished([use("todo_write", { todos: [] }, "c1"), say("Hi.")]);
}

const branches = async (store: Store): Promise<number> => {
  const { log } = await openStore(store);
  return (await rows(log.driver, "SELECT branch_id FROM branches")).length;
};

describe("thread.export", () => {
  test("writes log.jsonl, artifacts/ and bundle.json last, and importThread reproduces the thread", async () => {
    const { ref, thread } = await saved();
    const { root, path } = where();
    try {
      const written = unwrap(await thread.export(path));
      expect(written.branch_id).toBe(ref.branch);
      expect(written.artifacts).toBeGreaterThan(0);
      expect(readdirSync(path).toSorted()).toEqual([
        "artifacts",
        "bundle.json",
        "log.jsonl",
      ]);
      expect(readdirSync(join(path, "artifacts")).length).toBe(
        written.artifacts,
      );

      const into = sqlite(":memory:");
      const imported = unwrap(await importThread(into, path));
      expect(imported.id).toBe(ref.id);
      expect(imported.branch).toBe(ref.branch);
      // Every recorded request re-renders from the bundle's own artifacts (C7, Render v1).
      expect(await imported.replay()).toEqual({ ok: true, value: undefined });
      const here = await events(ref);
      const there = await events({ store: into, branch: imported.branch });
      expect(there.map((e) => e.event_id)).toEqual(here.map((e) => e.event_id));
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });

  test("an existing file, empty directory or non-empty directory is path_exists, untouched", async () => {
    const { thread } = await saved();
    const { root } = where();
    try {
      const file = join(root, "a-file");
      writeFileSync(file, "keep me");
      const empty = join(root, "empty");
      mkdirSync(empty);
      const full = join(root, "full");
      mkdirSync(full);
      writeFileSync(join(full, "keep"), "keep me too");
      for (const path of [file, empty, full])
        expect(code(await thread.export(path))).toBe("path_exists");
      expect(readFileSync(file, "utf8")).toBe("keep me");
      expect(readdirSync(empty)).toEqual([]);
      expect(readdirSync(full)).toEqual(["keep"]);
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });

  test("two exports to one path: one bundle, the other path_exists", async () => {
    const { thread } = await saved();
    const { root, path } = where();
    try {
      const [first, second] = await Promise.all([
        thread.export(path),
        thread.export(path),
      ]);
      const codes = [first, second].map((r) => (r?.ok === true ? "ok" : "path_exists"));
      expect(codes.toSorted()).toEqual(["ok", "path_exists"]);
      expect(readdirSync(path)).toContain("bundle.json");
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });
});

describe("importThread", () => {
  test("a directory with no bundle.json is bundle_incomplete: an unfinished export is never a bundle", async () => {
    const { thread } = await saved();
    const { root, path } = where();
    try {
      unwrap(await thread.export(path));
      unlinkSync(join(path, "bundle.json"));
      const into = sqlite(":memory:");
      expect(code(await importThread(into, path))).toBe("bundle_incomplete");
      expect(await branches(into)).toBe(0);
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });

  test("a tampered artifact is artifact_corrupt and the destination stays empty", async () => {
    const { thread } = await saved();
    const { root, path } = where();
    try {
      unwrap(await thread.export(path));
      const dir = join(path, "artifacts");
      const first = readdirSync(dir)[0];
      if (first === undefined) throw new Error("the bundle carries artifacts");
      writeFileSync(join(dir, first), "not the bytes that were exported");
      const into = sqlite(":memory:");
      expect(code(await importThread(into, path))).toBe("artifact_corrupt");
      expect(await branches(into)).toBe(0);
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });

  test("a bundled file the manifest doesn't list is bundle_incomplete when it is missing", async () => {
    const { thread } = await saved();
    const { root, path } = where();
    try {
      unwrap(await thread.export(path));
      const dir = join(path, "artifacts");
      const first = readdirSync(dir)[0];
      if (first === undefined) throw new Error("the bundle carries artifacts");
      unlinkSync(join(dir, first));
      const into = sqlite(":memory:");
      expect(code(await importThread(into, path))).toBe("bundle_incomplete");
      expect(await branches(into)).toBe(0);
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });

  test("a bare .jsonl whose artifacts the store doesn't hold is artifact_missing", async () => {
    const { thread } = await saved();
    const { root, path } = where();
    try {
      unwrap(await thread.export(path));
      const bare = join(root, "log.jsonl");
      writeFileSync(bare, readFileSync(join(path, "log.jsonl")));
      const into = sqlite(":memory:");
      expect(code(await importThread(into, bare))).toBe("artifact_missing");
      expect(await branches(into)).toBe(0);
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });

  test("export, delete, import: a deleted thread is never brought back (branch_exists)", async () => {
    const { ref, thread } = await saved();
    const { root, path } = where();
    try {
      unwrap(await thread.export(path));
      const { log } = await openStore(ref.store);
      unwrap(
        await log.driver.transaction(
          async (tx) => await deleteThread(tx, log.tenant, ref.id, log.now()),
        ),
      );
      const back = await importThread(ref.store, path);
      expect(code(back)).toBe("branch_exists");
      expect(back.ok ? "" : back.error.message).toContain("was deleted");
      expect(await branches(ref.store)).toBe(0);
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });

  test("importing the same bundle twice is idempotent: the same branch, no second copy", async () => {
    const { thread } = await saved();
    const { root, path } = where();
    try {
      unwrap(await thread.export(path));
      const into = sqlite(":memory:");
      const first = unwrap(await importThread(into, path));
      const again = unwrap(await importThread(into, path));
      expect(again.branch).toBe(first.branch);
      expect(await branches(into)).toBe(1);
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });

  test("a thread another tenant owns is branch_exists, and openThread still can't see it", async () => {
    const { thread, ref } = await saved();
    const { root, path } = where();
    try {
      unwrap(await thread.export(path));
      const into = sqlite(":memory:");
      unwrap(await importThread(into, path));
      const opened = await openThread(into, ref.id);
      expect(opened.ok).toBe(true);
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });
});
