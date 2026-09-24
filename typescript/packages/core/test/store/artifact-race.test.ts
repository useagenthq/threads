import { afterEach, describe, expect, test } from "bun:test";
import {
  existsSync,
  mkdtempSync,
  readdirSync,
  renameSync,
  statSync,
  utimesSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileArtifacts } from "../../src/store";
import {
  artifactSeams,
  type SeamPoint,
  trashName,
} from "../../src/store/artifact-trash";
import { StoreError } from "../../src/store/driver";
import { sweepFiles } from "../../src/store/retention";

// spec/schema/README.md, "Artifacts": a put refreshes and re-links, gc deletes only trash
// names, a reader restores from trash. Each interleaving of plans/specs/lanes/24 Tests 4a is
// forced through the store's test seams; the control runs the old behaviour into the same
// interleavings and loses the artifact.

const bytes = new TextEncoder().encode('{"name":"mcp__jira__create_issue"}');
const DAY = 86_400_000;

afterEach(() => {
  artifactSeams.hook = undefined;
  artifactSeams.rules = true;
});

function setup() {
  const root = mkdtempSync(join(tmpdir(), "threads-race-"));
  const store = fileArtifacts(root);
  const sha = store.put(bytes);
  const dir = join(root, "sha256", sha.slice(0, 2));
  const path = join(dir, sha);
  backdate(path);
  return { root, store, sha, dir, path, cutoff: Date.now() - DAY };
}

function backdate(path: string): void {
  const then = new Date(Date.now() - 30 * DAY);
  utimesSync(path, then, then);
}

/** Runs `step` the first time the store reaches `point`. */
function at(point: SeamPoint, step: () => void): void {
  let fired = false;
  artifactSeams.hook = (p) => {
    if (p !== point || fired) return;
    fired = true;
    step();
  };
}

function trash(dir: string): readonly string[] {
  return readdirSync(dir).filter((n) => n.includes(".trash-"));
}

function code(r: ReturnType<ReturnType<typeof fileArtifacts>["get"]>): string {
  return r.ok ? "ok" : r.error.code;
}

describe("the artifact store survives a concurrent gc", () => {
  test("a put of an existing artifact refreshes its mtime before it verifies", () => {
    const { store, path } = setup();
    let seen = 0;
    at("put:verify", () => {
      seen = statSync(path).mtimeMs;
    });
    store.put(bytes);
    expect(seen).toBeGreaterThan(Date.now() - DAY);
  });

  test("easy order: re-put spec artifacts survive a gc before thread_started", () => {
    const { root, store, sha, cutoff } = setup();
    // Thread A named the artifact and was deleted: nothing references it any more.
    store.put(bytes); // thread B re-puts before its thread_started
    expect(sweepFiles(root, new Set(), cutoff)).toEqual([]);
    expect(code(store.get(sha))).toBe("ok");
  });

  test("I1: gc stats it old, a put refreshes it, gc renames, sees it young, restores it", () => {
    const { root, store, sha, dir, cutoff } = setup();
    at("gc:stated", () => store.put(bytes));
    expect(sweepFiles(root, new Set(), cutoff)).toEqual([]);
    expect(code(store.get(sha))).toBe("ok");
    expect(trash(dir)).toEqual([]);
  });

  test("I2: gc renames during a put's refresh; the put re-links its own copy", () => {
    const { root, store, sha, path, cutoff } = setup();
    at("put:exists", () => {
      expect(sweepFiles(root, new Set(), cutoff)).toEqual([sha]);
    });
    expect(store.put(bytes)).toBe(sha);
    expect(existsSync(path)).toBe(true);
    expect(code(store.get(sha))).toBe("ok");
  });

  test("I3: gc renames between a put's refresh and its verify; the put re-links", () => {
    const { store, sha, dir, path } = setup();
    at("put:verify", () => renameSync(path, join(dir, trashName(sha))));
    expect(store.put(bytes)).toBe(sha);
    expect(code(store.get(sha))).toBe("ok");
  });

  test("I4: a reader restores from trash; gc unlinks only the trash name", () => {
    const { root, store, sha, path, cutoff } = setup();
    at("gc:renamed", () => {
      expect(code(store.get(sha))).toBe("ok");
    });
    expect(sweepFiles(root, new Set(), cutoff)).toEqual([sha]);
    expect(existsSync(path)).toBe(true);
    expect(code(store.get(sha))).toBe("ok");
  });

  test("I5: a gc killed after its rename leaves trash; get restores, a later gc clears it", () => {
    const { root, store, sha, dir, path, cutoff } = setup();
    at("gc:renamed", () => {
      throw new Error("killed");
    });
    expect(() => sweepFiles(root, new Set(), cutoff)).toThrow("killed");
    expect(existsSync(path)).toBe(false);
    expect(code(store.get(sha))).toBe("ok");
    for (const name of trash(dir)) backdate(join(dir, name));
    backdate(path);
    expect(sweepFiles(root, new Set([sha]), cutoff)).toEqual([]);
    expect(trash(dir)).toEqual([]);
    expect(code(store.get(sha))).toBe("ok");
  });
});

describe("control: without the rules, each interleaving loses the artifact", () => {
  test("I1", () => {
    const { root, store, sha, cutoff } = setup();
    artifactSeams.rules = false;
    at("gc:stated", () => store.put(bytes));
    sweepFiles(root, new Set(), cutoff);
    expect(code(store.get(sha))).toBe("artifact_missing");
  });

  test("I2", () => {
    const { root, store, sha, cutoff } = setup();
    artifactSeams.rules = false;
    at("put:exists", () => sweepFiles(root, new Set(), cutoff));
    expect(() => store.put(bytes)).toThrow(StoreError);
    expect(code(store.get(sha))).toBe("artifact_missing");
  });

  test("I3", () => {
    const { store, sha, dir, path } = setup();
    artifactSeams.rules = false;
    at("put:verify", () => renameSync(path, join(dir, trashName(sha))));
    expect(() => store.put(bytes)).toThrow(StoreError);
    expect(code(store.get(sha))).toBe("artifact_missing");
  });

  test("I4", () => {
    const { root, store, sha, cutoff } = setup();
    artifactSeams.rules = false;
    at("gc:stated", () => {
      expect(code(store.get(sha))).toBe("ok");
    });
    sweepFiles(root, new Set(), cutoff);
    expect(code(store.get(sha))).toBe("artifact_missing");
  });
});

test("re-putting 500 existing artifacts creates no new file or link", () => {
  const root = mkdtempSync(join(tmpdir(), "threads-reput-"));
  const store = fileArtifacts(root);
  const all = Array.from({ length: 500 }, (_, i) =>
    new TextEncoder().encode(`spec ${i}`),
  );
  for (const b of all) store.put(b);
  const files = (): readonly number[] =>
    readdirSync(join(root, "sha256"), { recursive: true, encoding: "utf8" })
      .map((n) => statSync(join(root, "sha256", n)))
      .filter((s) => s.isFile())
      .map((s) => s.nlink);
  const before = files();
  for (const b of all) store.put(b);
  expect(files()).toEqual(before);
  expect(before.length).toBe(500);
  expect(before.every((n) => n === 1)).toBe(true);
  // 1000 fsynced puts: slow on a bind-mounted Linux file system, well past the 5 s default.
}, 60_000);
