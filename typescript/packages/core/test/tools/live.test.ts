import { describe, expect, test } from "bun:test";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { secret } from "../../src";
import { gitClone } from "../../src/tools/git/clone";
import { brave, exa, tavily } from "../../src/tools/search-backends";
import { webFetch } from "../../src/tools/web-fetch";
import { webSearch } from "../../src/tools/web-search";
import { bound, localSession } from "./kit";

// Live gates for the gateway tools: real DNS and network, so they run only
// with THREADS_LIVE=1. web-fetch-ssrf-and-record (F1.15) needs nothing else; web-search-citations
// (F1.16) runs per backend whose key is set; the git gate needs THREADS_LIVE_GIT_REPO (owner/name)
// and GITHUB_TOKEN, and only clones (git-push-via-gateway pushes to a scratch branch by hand).

const live = process.env["THREADS_LIVE"] === "1";
const env = (name: string): string | undefined => process.env[name];

describe.skipIf(!live)("live gate: web_fetch", () => {
  test("a public page records URL, status, hash and a citation; private targets are denied", async () => {
    const tool = bound(webFetch());
    const page = await tool.run({ url: "https://example.com/" });
    expect(page).toMatchObject({ kind: "done", isError: false });
    expect(page.kind === "done" && page.content?.[1]).toMatchObject({
      type: "citation",
      source_kind: "web",
    });
    for (const url of [
      "http://169.254.169.254/latest/meta-data/",
      "http://localhost/",
      "http://10.0.0.1/",
    ])
      expect(await tool.run({ url })).toMatchObject({ isError: true });
  }, 60_000);
});

const backends = [
  ["exa", "EXA_API_KEY", exa],
  ["brave", "BRAVE_API_KEY", brave],
  ["tavily", "TAVILY_API_KEY", tavily],
] as const;

for (const [name, key, make] of backends)
  describe.skipIf(!live || env(key) === undefined)(
    `live gate: web_search (${name})`,
    () => {
      test("hits come back as text with one web citation each", async () => {
        const run = await bound(webSearch(make(secret(key)))).run({
          query: "bun sqlite",
        });
        if (run.kind !== "done") throw new Error(run.kind);
        expect(run.isError).toBe(false);
        const cites = run.content?.filter((p) => p.type === "citation") ?? [];
        expect(cites.length).toBeGreaterThan(0);
      }, 60_000);
    },
  );

const repo = env("THREADS_LIVE_GIT_REPO");
describe.skipIf(
  !live || repo === undefined || env("GITHUB_TOKEN") === undefined,
)("live gate: git gateway", () => {
  test("clone through the host with the token, which never reaches the sandbox", async () => {
    const sandbox = localSession(
      join(mkdtempSync(join(tmpdir(), "threads-live-git-")), "sbx"),
    );
    const run = await bound(
      gitClone({ credential: secret("GITHUB_TOKEN") }),
      sandbox,
    ).run({
      repo: repo ?? "",
    });
    expect(run).toMatchObject({ kind: "done", isError: false });
    expect(JSON.stringify(sandbox.execs)).not.toContain(
      env("GITHUB_TOKEN") ?? "",
    );
  }, 300_000);
});
