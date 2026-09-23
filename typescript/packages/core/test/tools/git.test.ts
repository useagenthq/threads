import { afterAll, beforeAll, describe, expect, test } from "bun:test";
import {
  mkdtempSync,
  readdirSync,
  readFileSync,
  rmSync,
  statSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { secret } from "../../src";
import { gitClone, gitFetch } from "../../src/tools/git/clone";
import type { GitOptions } from "../../src/tools/git/host";
import { forgeFetch, openPullRequest } from "../../src/tools/git/pull-request";
import { gitPush } from "../../src/tools/git/push";
import { bound, type LocalSession, localSession } from "./kit";

// The git gateway (F11.12): host git with the credential, bundles in and out of
// a sandbox that never sees it. The forge is a local bare repository: no network.

const TOKEN = "ghs_canary_0123456789";
const VAR = "THREADS_TEST_GIT_TOKEN";
let root = "";
let options: GitOptions;
let sandbox: LocalSession;

function sh(cwd: string, ...args: string[]): string {
  const ran = Bun.spawnSync(
    ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", ...args],
    {
      cwd,
      env: {
        PATH: process.env["PATH"] ?? "/usr/bin:/bin",
        HOME: root,
        GIT_CONFIG_NOSYSTEM: "1",
      },
    },
  );
  if (ran.exitCode !== 0)
    throw new Error(`git ${args.join(" ")}: ${ran.stderr.toString()}`);
  return ran.stdout.toString().trim();
}

/** Every file's bytes under a directory, as text, for the canary. */
function everything(dir: string): string {
  return readdirSync(dir, { recursive: true, encoding: "utf8" })
    .map((p) => join(dir, p))
    .filter((p) => statSync(p).isFile())
    .map((p) => readFileSync(p).toString("latin1"))
    .join("\n");
}

beforeAll(() => {
  root = mkdtempSync(join(tmpdir(), "threads-git-test-"));
  process.env[VAR] = TOKEN;
  sh(
    root,
    "init",
    "--quiet",
    "--bare",
    "--initial-branch=main",
    "forge/acme/api.git",
  );
  sh(root, "init", "--quiet", "--initial-branch=main", "seed");
  Bun.write(join(root, "seed", "README.md"), "hello\n");
  sh(join(root, "seed"), "add", ".");
  sh(join(root, "seed"), "commit", "--quiet", "-m", "init");
  sh(
    join(root, "seed"),
    "push",
    "--quiet",
    join(root, "forge/acme/api.git"),
    "main",
  );
  options = { credential: secret(VAR), forgeUrl: `file://${root}/forge` };
  sandbox = localSession(join(root, "sbx"));
});

afterAll(() => {
  delete process.env[VAR];
  rmSync(root, { recursive: true, force: true });
});

describe("git gateway", () => {
  test("clone, commit in the sandbox, push from the host; the credential never enters the sandbox", async () => {
    const clone = await bound(gitClone(options), sandbox).run({
      repo: "acme/api",
    });
    expect(clone).toMatchObject({ kind: "done", isError: false });
    const dir = join(root, "sbx/workspace/api");
    expect(readFileSync(join(dir, "README.md"), "utf8")).toBe("hello\n");
    expect(sh(dir, "remote", "get-url", "origin")).toBe(
      `file://${root}/forge/acme/api.git`,
    );

    sh(dir, "checkout", "--quiet", "-b", "fix-1");
    Bun.write(join(dir, "fix.txt"), "fixed\n");
    sh(dir, "add", ".");
    sh(dir, "commit", "--quiet", "-m", "fix");
    const head = sh(dir, "rev-parse", "HEAD");

    const push = bound(gitPush(options), sandbox);
    const pushed = await push.run({ repo: "acme/api", branch: "fix-1" });
    expect(pushed).toEqual({
      kind: "done",
      output: `pushed ${head} to acme/api fix-1`,
      isError: false,
      receipt: head,
    });
    expect(
      sh(join(root, "forge/acme/api.git"), "rev-parse", "refs/heads/fix-1"),
    ).toBe(head);

    // A crash mid-push reconciles by the remote ref.
    const lookup = push.impl.reconcile;
    expect(lookup?.finality).toBe("nonfinal");
    expect(
      await lookup?.lookup("k", { repo: "acme/api", branch: "fix-1" }),
    ).toEqual({
      status: "found",
      value: `pushed ${head} to acme/api fix-1`,
    });
    sh(dir, "checkout", "--quiet", "-b", "fix-2");
    Bun.write(join(dir, "two.txt"), "2\n");
    sh(dir, "add", ".");
    sh(dir, "commit", "--quiet", "-m", "two");
    expect(
      await lookup?.lookup("k", { repo: "acme/api", branch: "fix-2" }),
    ).toEqual({ status: "not_found" });

    // The canary (F11.2): no token in any sandbox file, argv or env.
    expect(everything(join(root, "sbx"))).not.toContain(TOKEN);
    expect(JSON.stringify(sandbox.execs)).not.toContain(TOKEN);
    expect(sandbox.execs.every((e) => Object.keys(e.env).length === 0)).toBe(
      true,
    );
  });

  test("fetch brings new forge commits in as origin/*", async () => {
    sh(
      join(root, "seed"),
      "commit",
      "--quiet",
      "--allow-empty",
      "-m",
      "upstream",
    );
    sh(
      join(root, "seed"),
      "push",
      "--quiet",
      join(root, "forge/acme/api.git"),
      "main",
    );
    const want = sh(join(root, "seed"), "rev-parse", "HEAD");
    const run = await bound(gitFetch(options), sandbox).run({
      repo: "acme/api",
      ref: "main",
    });
    expect(run).toMatchObject({ kind: "done", isError: false });
    expect(
      sh(
        join(root, "sbx/workspace/api"),
        "rev-parse",
        "refs/remotes/origin/main",
      ),
    ).toBe(want);
  });

  test("a rejected push is an error result; bad refs and paths are refused before anything runs", async () => {
    const dir = join(root, "sbx/workspace/api");
    sh(dir, "checkout", "--quiet", "-B", "main", "HEAD~2");
    Bun.write(join(dir, "diverge.txt"), "x\n");
    sh(dir, "add", ".");
    sh(dir, "commit", "--quiet", "-m", "diverge");
    const push = bound(gitPush(options), sandbox);
    const rejected = await push.run({ repo: "acme/api", branch: "main" });
    expect(rejected).toMatchObject({ kind: "done", isError: true });
    const before = sandbox.execs.length;
    expect(
      await push.run({ repo: "acme/api", branch: "--force" }),
    ).toMatchObject({ isError: true });
    expect(
      await push.run({ repo: "acme/api", branch: "x", path: "../../etc" }),
    ).toMatchObject({ isError: true });
    expect(
      await bound(gitClone(options), sandbox).run({
        repo: "acme/api",
        ref: "a..b",
      }),
    ).toMatchObject({ isError: true });
    expect(sandbox.execs.length).toBe(before);
  });

  test("a missing credential is an error result and nothing is sent", async () => {
    const run = await bound(
      gitClone({ ...options, credential: secret("THREADS_TEST_UNSET") }),
      sandbox,
    ).run({ repo: "acme/api" });
    expect(run).toMatchObject({ kind: "done", isError: true });
    expect(run.kind === "done" && run.output.startsWith("missing_secret")).toBe(
      true,
    );
  });
});

describe("open_pull_request", () => {
  const forge = (answers: Record<string, Response | (() => Response)>) => {
    const seen: { url: string; method: string; auth: string | null }[] = [];
    const fetch = async (url: string, init: RequestInit): Promise<Response> => {
      const method = init.method ?? "GET";
      seen.push({
        url,
        method,
        auth: new Headers(init.headers).get("authorization"),
      });
      const answer = answers[`${method} ${url}`];
      if (answer === undefined) throw new Error(`unexpected ${method} ${url}`);
      return typeof answer === "function" ? answer() : answer;
    };
    return { seen, o: { ...options, apiUrl: "https://forge.test", fetch } };
  };
  const PR = { repo: "acme/api", head: "fix-1", base: "main", title: "Fix" };
  const LIST =
    "GET https://forge.test/repos/acme/api/pulls?head=acme%3Afix-1&base=main&state=all&sort=created&direction=desc";
  const POST = "POST https://forge.test/repos/acme/api/pulls";
  const pull = {
    html_url: "https://forge.test/acme/api/pull/7",
    number: 7,
    state: "open",
    merged_at: null,
  };
  const closed = {
    ...pull,
    number: 3,
    state: "closed",
    html_url: "https://forge.test/acme/api/pull/3",
  };
  const EXISTS = {
    message: "Validation Failed",
    errors: [
      {
        resource: "PullRequest",
        code: "custom",
        message: "A pull request already exists for acme:fix-1.",
      },
    ],
  };

  test("looks up (head, base) first, then creates one; the token goes only in the host's request", async () => {
    const f = forge({
      [LIST]: Response.json([]),
      [POST]: Response.json(pull, { status: 201 }),
    });
    const run = await bound(openPullRequest(f.o)).run(PR);
    expect(run).toEqual({
      kind: "done",
      output: "pull request #7 (open): https://forge.test/acme/api/pull/7",
      isError: false,
      receipt: "7",
    });
    expect(f.seen.map((s) => s.method)).toEqual(["GET", "POST"]);
    expect(f.seen.every((s) => s.auth === `Bearer ${TOKEN}`)).toBe(true);
  });

  test("an existing one in any state is the result, and nothing is created", async () => {
    const f = forge({ [LIST]: () => Response.json([closed]) });
    expect(await bound(openPullRequest(f.o)).run(PR)).toMatchObject({
      isError: false,
      output: "pull request #3 (closed): https://forge.test/acme/api/pull/3",
    });
    expect(f.seen.some((s) => s.method === "POST")).toBe(false);
    const merged = forge({
      [LIST]: Response.json([{ ...closed, merged_at: "2026-01-01T00:00:00Z" }]),
    });
    expect(await bound(openPullRequest(merged.o)).run(PR)).toMatchObject({
      output: "pull request #3 (merged): https://forge.test/acme/api/pull/3",
    });
  });

  test("a 422 means 'already exists' only when the forge says so; any other 422 is an error", async () => {
    let listed = 0;
    const raced = forge({
      [LIST]: () => Response.json(listed++ === 0 ? [] : [pull]),
      [POST]: () => Response.json(EXISTS, { status: 422 }),
    });
    expect(await bound(openPullRequest(raced.o)).run(PR)).toMatchObject({
      isError: false,
      output: "pull request #7 (open): https://forge.test/acme/api/pull/7",
    });
    const badBase = forge({
      [LIST]: () => Response.json([]),
      [POST]: () =>
        Response.json(
          {
            message: "Validation Failed",
            errors: [{ field: "base", code: "invalid" }],
          },
          { status: 422 },
        ),
    });
    const run = await bound(openPullRequest(badBase.o)).run(PR);
    expect(run).toMatchObject({ kind: "done", isError: true });
    expect(
      run.kind === "done" && run.output.startsWith("the forge refused"),
    ).toBe(true);
  });

  test("a server error is unknown; recovery looks up (head, base) in every state", async () => {
    const down = forge({
      [LIST]: Response.json([]),
      [POST]: new Response("bad gateway", { status: 502 }),
    });
    const tool = bound(openPullRequest(down.o));
    expect(await tool.run(PR)).toEqual({
      kind: "unknown",
      reason: "transport_error",
    });
    expect(tool.impl.reconcile?.finality).toBe("final");
    const later = forge({
      [LIST]: Response.json([{ ...pull, state: "closed" }]),
    });
    expect(
      await bound(openPullRequest(later.o)).impl.reconcile?.lookup("k", PR),
    ).toEqual({
      status: "found",
      value: "pull request #7 (closed): https://forge.test/acme/api/pull/7",
    });
  });

  test("the forge API goes through the SSRF guard: a private api_url is never contacted", async () => {
    const res = await forgeFetch("https://169.254.169.254/repos/a/b/pulls", {
      method: "GET",
    });
    expect(res.status).toBe(403);
    expect(await res.text()).toContain("non-public");
  });
});
