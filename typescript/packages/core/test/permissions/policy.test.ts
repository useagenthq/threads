import { describe, expect, test } from "bun:test";
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { z } from "zod";
import { agent, scriptedModel } from "../../src";
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
    thread_rules: z
      .array(
        z.strictObject({
          rule: z.string(),
          decision: z.enum(["allow", "deny"]),
        }),
      )
      .default([]),
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
const Expected = z.discriminatedUnion("outcome", [
  z.strictObject({
    outcome: z.literal("ok"),
    decisions: z.array(
      z.strictObject({
        decision: z.enum(["allow", "deny", "ask"]),
        source: z.string(),
        rule: z.string().optional(),
      }),
    ),
  }),
  z.strictObject({
    outcome: z.literal("error"),
    error: z.strictObject({ code: z.literal("permission_rule_invalid") }),
  }),
]);

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
  for (const name of policyCases) {
    test(name, async () => {
      const { input } = PolicyCase.parse(read(name, "case.json"));
      const expected = Expected.parse(read(name, "expected.json"));
      if (expected.outcome === "error") {
        const checked = await agent({
          model: scriptedModel({ responses: [] }),
          permissions: input.permissions,
        }).check();
        expect(checked).toMatchObject({
          ok: false,
          error: { code: expected.error.code },
        });
        return;
      }
      const got = input.calls.map((call) =>
        input.ceiling === undefined
          ? decide(input.permissions, input.workspace, call, input.thread_rules)
          : decideCapped(
              input.permissions,
              input.ceiling,
              input.workspace,
              call,
            ),
      );
      expect<unknown>(got).toEqual(expected.decisions);
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
  test("only the literal bash(*) is the any-command rule, not a tool pattern", () => {
    const pipeline = "cd app && npm test 2>&1 | tail -50";
    expect(bash(pipeline, ["b*(*)"]).decision).toBe("ask");
    expect(bash(pipeline, [], ["b*(*)"]).decision).toBe("ask");
    expect(bash(pipeline, ["bash(*)"]).decision).toBe("allow");
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

// Shell structures a denied command can hide in; `%` is where the inner command goes.
const NESTINGS = [
  "if true; then %; fi",
  "if %; then true; fi",
  "while true; do %; done",
  "until %; do true; done",
  "for x in a b; do %; done",
  "case a in a) %;; esac",
  "( % )",
  "{ %; }",
  "! %",
  "coproc %",
  "echo $(%)",
  "echo `%`",
  "f() { %; }",
  "true && %",
  "% | tail",
];
const DENIED = [
  "rm -rf /",
  "r''m -rf /",
  "FOO=1 rm x",
  "nice -n 5 rm x",
  "timeout 5 rm x",
];

describe("a deny sees a command at any depth", () => {
  const nested = (inner: string): readonly string[] => [
    inner,
    ...NESTINGS.flatMap((a) => {
      const once = a.replace("%", inner);
      return [once, ...NESTINGS.map((b) => b.replace("%", once))];
    }),
  ];
  test.each(DENIED)("%p, nested up to two deep", (inner) => {
    const allow = ["bash", "bash(true)", "bash(echo:*)"];
    for (const command of nested(inner)) {
      const got = decide(
        { ...DEFAULT_PERMISSIONS, mode: "bypass", allow, deny: ["bash(rm:*)"] },
        "/workspace",
        { tool: "bash", category: "other", mode: "bypass", input: { command } },
      );
      expect([command, got.decision, got.rule]).toEqual([
        command,
        "deny",
        "bash(rm:*)",
      ]);
    }
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
