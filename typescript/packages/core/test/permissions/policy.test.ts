import { describe, expect, test } from "bun:test";
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { z } from "zod";
import { PermissionMode, PermissionsPolicy } from "../../src/log";
import {
  category,
  DEFAULT_PERMISSIONS,
  decide,
  decideCapped,
} from "../../src/permissions";

// Runs every `policy` case in spec/conformance/cases.

const CASES = join(import.meta.dir, "../../../../../spec/conformance/cases");

const PolicyCase = z.object({
  kind: z.literal("policy"),
  input: z.strictObject({
    workspace: z.string(),
    permissions: PermissionsPolicy,
    ceiling: PermissionsPolicy.optional(),
    calls: z.array(
      z.strictObject({
        mode: PermissionMode,
        tool: z.string(),
        category: z.enum(["read_only", "edit", "other"]),
        input: z.record(z.string(), z.unknown()),
      }),
    ),
  }),
});
const Expected = z.strictObject({
  outcome: z.literal("ok"),
  decisions: z.array(
    z.strictObject({
      decision: z.enum(["allow", "deny", "ask"]),
      source: z.string(),
      rule: z.string().optional(),
    }),
  ),
});

const read = (name: string, file: string): unknown =>
  JSON.parse(readFileSync(join(CASES, name, file), "utf8"));

const policyCases = readdirSync(CASES).filter(
  (name) =>
    z.object({ kind: z.string() }).parse(read(name, "case.json")).kind ===
    "policy",
);

describe("policy conformance", () => {
  test("the corpus has policy cases", () => {
    expect(policyCases.length).toBeGreaterThanOrEqual(4);
  });
  // Removed by the change that implements it; skipped by name, never silently.
  const pending = new Set(["skills-write-denied-all-paths"]);
  for (const name of policyCases) {
    test.skipIf(pending.has(name))(name, () => {
      const { input } = PolicyCase.parse(read(name, "case.json"));
      const { decisions } = Expected.parse(read(name, "expected.json"));
      const got = input.calls.map((call) =>
        input.ceiling === undefined
          ? decide(input.permissions, input.workspace, call)
          : decideCapped(
              input.permissions,
              input.ceiling,
              input.workspace,
              call,
            ),
      );
      expect<unknown>(got).toEqual(decisions);
    });
  }
});

const bash = (
  command: string,
  allow: readonly string[],
  deny: readonly string[] = [],
) =>
  decide(
    { ...DEFAULT_PERMISSIONS, allow: [...allow], deny: [...deny] },
    "/workspace",
    {
      tool: "bash",
      category: "other",
      mode: "default",
      input: { command },
    },
  );

describe("conservative shell rules", () => {
  test.each([
    "ls $(whoami)",
    "ls `whoami`",
    "(ls)",
    "{ ls; }",
    "eval ls",
    "exec ls",
    "cat <<EOF",
    "ls 'unclosed",
  ])("%p is never allowed", (command) => {
    expect(bash(command, ["bash", "bash(ls:*)", "bash(cat:*)"]).decision).toBe(
      "ask",
    );
  });
  // an escaped quote must not hide a separator inside one word.
  test.each([
    'git status \\"; printf marker; echo \\"',
    "git status \\; printf marker",
    'git status "\\"; printf marker; echo "',
    "git status `printf marker`",
    'git status "$(printf marker)"',
    "git status\nprintf marker",
    "git status && printf marker",
    "git status \u0024{IFS}x",
    "git status > /tmp/x",
    "git status # ; printf marker",
  ])("%p is not allowed by a git status prefix rule", (command) => {
    expect(bash(command, ["bash(git status:*)"]).decision).toBe("ask");
  });
  test("a deny still sees a command hidden behind an escaped quote", () => {
    expect(
      bash('git status \\"; rm -rf /; echo \\"', [], ["bash(rm:*)"]).decision,
    ).toBe("deny");
  });
  test("single quotes keep separators literal and still allow", () => {
    expect(bash("git status 'a;b'", ["bash(git status:*)"]).decision).toBe(
      "allow",
    );
  });
  test("a non-string command matches no specifier rule", () => {
    const got = decide(
      { ...DEFAULT_PERMISSIONS, allow: ["bash(ls:*)"] },
      "/w",
      {
        tool: "bash",
        category: "other",
        mode: "default",
        input: { command: 42 },
      },
    );
    expect(got).toEqual({ decision: "ask", source: "mode" });
  });
  test("a deny inside a quoted pipeline still matches its simple command", () => {
    expect(
      bash("echo hi | rm -rf /", ["bash(echo:*)"], ["bash(rm:*)"]).decision,
    ).toBe("deny");
  });
  test("a path that escapes the workspace never matches a relative rule", () => {
    const got = decide(
      { ...DEFAULT_PERMISSIONS, allow: ["read(**)"] },
      "/workspace",
      {
        tool: "read",
        category: "read_only",
        mode: "default",
        input: { path: "../../etc/passwd" },
      },
    );
    expect(got).toEqual({ decision: "ask", source: "mode" });
  });
});

const file = (
  tool: string,
  path: string,
  rules: { readonly allow?: string[]; readonly deny?: string[] },
) =>
  decide(
    {
      ...DEFAULT_PERMISSIONS,
      allow: rules.allow ?? [],
      deny: rules.deny ?? [],
    },
    "/workspace",
    {
      tool,
      category: tool === "read" ? "read_only" : "edit",
      mode: "default",
      input: { path },
    },
  );

// gitignore(5) anchoring and directory patterns, the same for allow and deny.
describe("path rules follow gitignore", () => {
  test("a leading slash anchors to the workspace root", () => {
    expect(file("write", "safe.txt", { allow: ["write(/safe.txt)"] })).toEqual({
      decision: "allow",
      source: "policy",
      rule: "write(/safe.txt)",
    });
    expect(
      file("write", "nested/safe.txt", { allow: ["write(/safe.txt)"] }),
    ).toEqual({ decision: "ask", source: "mode" });
  });
  test("a trailing slash covers the directory and every descendant", () => {
    for (const path of ["secrets/token", "a/secrets/deep/token"])
      expect(file("read", path, { deny: ["read(secrets/)"] })).toEqual({
        decision: "deny",
        source: "policy",
        rule: "read(secrets/)",
      });
  });
  test("a pattern naming a directory covers its contents, for allow and deny alike", () => {
    expect(file("read", "docs/a/b.md", { deny: ["read(docs)"] }).decision).toBe(
      "deny",
    );
    expect(
      file("edit", "docs/a/b.md", { allow: ["edit(docs)"] }).decision,
    ).toBe("allow");
  });
});

describe("category", () => {
  test.each([
    ["read", "read_only", "read_only"],
    ["edit", "sandbox_local", "edit"],
    ["web_fetch", "read_only", "other"],
    ["send_email", undefined, "other"],
  ] as const)("%s (%s) is %s", (tool, effect, want) => {
    expect(category(tool, effect)).toBe(want);
  });
});
