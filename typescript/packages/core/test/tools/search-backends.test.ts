import { afterAll, beforeAll, describe, expect, test } from "bun:test";
import { agent, scriptedModel, secret } from "../../src";
import { brave, exa, tavily } from "../../src/tools/search-backends";
import {
  liveTransport,
  type Sent,
  type WebTransport,
} from "../../src/tools/web-transport";

// The web_search backends and the pinned host transport. Backends are driven
// through a scripted transport; the live transport only ever talks to a local server here.

const VAR = "THREADS_TEST_SEARCH_KEY";
beforeAll(() => {
  process.env[VAR] = "sk-search";
});
afterAll(() => {
  delete process.env[VAR];
});

function scripted(answer: Response): WebTransport & {
  readonly sent: { url: string; address: string; init: Sent }[];
} {
  const sent: { url: string; address: string; init: Sent }[] = [];
  return {
    sent,
    resolve: async () => ["93.184.216.34"],
    fetch: async (url, address, init) => {
      sent.push({ url, address, init });
      return answer;
    },
  };
}

const opts = { allowedDomains: ["bun.sh"], blockedDomains: ["spam.example"] };

describe("search backends", () => {
  test("exa: one JSON POST with the key header and domain filters; hits mapped", async () => {
    const t = scripted(
      Response.json({
        results: [
          { url: "https://bun.sh/a", title: "A", text: "  alpha  ", extra: 1 },
          { url: "https://bun.sh/b", title: null, text: null },
        ],
      }),
    );
    const got = await exa(secret(VAR), t).search("q", opts);
    expect(got).toEqual({
      ok: true,
      value: [
        { url: "https://bun.sh/a", title: "A", snippet: "alpha" },
        { url: "https://bun.sh/b", title: "https://bun.sh/b", snippet: "" },
      ],
    });
    const [req] = t.sent;
    expect(req?.url).toBe("https://api.exa.ai/search");
    expect(req?.address).toBe("93.184.216.34");
    expect(req?.init.headers["x-api-key"]).toBe("sk-search");
    expect(JSON.parse(req?.init.body ?? "")).toEqual({
      query: "q",
      numResults: 10,
      contents: { text: true },
      includeDomains: ["bun.sh"],
      excludeDomains: ["spam.example"],
    });
  });

  test("tavily: bearer auth and include/exclude domains", async () => {
    const t = scripted(
      Response.json({
        results: [{ url: "https://bun.sh/x", title: "X", content: "c" }],
      }),
    );
    const got = await tavily(secret(VAR), t).search("q", {});
    expect(got).toEqual({
      ok: true,
      value: [{ url: "https://bun.sh/x", title: "X", snippet: "c" }],
    });
    expect(t.sent[0]?.init.headers["authorization"]).toBe("Bearer sk-search");
    expect(JSON.parse(t.sent[0]?.init.body ?? "")).toEqual({
      query: "q",
      max_results: 10,
    });
  });

  test("brave: a GET with site: operators and the subscription token", async () => {
    const t = scripted(
      Response.json({
        web: {
          results: [{ url: "https://bun.sh/y", title: "Y", description: "d" }],
        },
      }),
    );
    const got = await brave(secret(VAR), t).search("q", opts);
    expect(got).toEqual({
      ok: true,
      value: [{ url: "https://bun.sh/y", title: "Y", snippet: "d" }],
    });
    const url = new URL(t.sent[0]?.url ?? "");
    expect(url.searchParams.get("q")).toBe("q site:bun.sh -site:spam.example");
    expect(t.sent[0]?.init.headers["x-subscription-token"]).toBe("sk-search");
  });

  test("a non-200, an unreadable answer or a missing key is unavailable", async () => {
    expect(
      await exa(
        secret(VAR),
        scripted(new Response("x", { status: 429 })),
      ).search("q", {}),
    ).toMatchObject({
      ok: false,
      error: { code: "unavailable" },
    });
    expect(
      await exa(
        secret(VAR),
        scripted(Response.json({ results: [{ title: 1 }] })),
      ).search("q", {}),
    ).toMatchObject({
      ok: false,
    });
    expect(
      await exa(
        secret("THREADS_TEST_UNSET_KEY"),
        scripted(Response.json({})),
      ).search("q", {}),
    ).toMatchObject({
      ok: false,
    });
  });
});

// spec/api.json config_errors: <factory>!missing_secret@ts. The key resolves at setup, so
// check() reports it before any run, and nothing is sent.
describe("search backend refusals", () => {
  for (const [name, make] of [
    ["exa", exa],
    ["tavily", tavily],
    ["brave", brave],
  ] as const) {
    test(`${name}: check() reports an unset key as missing_secret`, async () => {
      const t = scripted(Response.json({}));
      const checked = await agent({
        model: scriptedModel({ responses: [] }),
        web: { search: make(secret("THREADS_TEST_UNSET_KEY"), t) },
      }).check();
      expect(checked).toEqual({
        ok: false,
        error: {
          code: "missing_secret",
          message: `${name}: set apiKey or THREADS_TEST_UNSET_KEY`,
        },
      });
      expect(t.sent).toEqual([]);
    });
  }

  test("a key resolved at setup is kept for the searches that follow", async () => {
    const t = scripted(Response.json({ results: [] }));
    const backend = exa(secret(VAR), t);
    await backend.setup?.();
    const before = process.env[VAR];
    delete process.env[VAR];
    try {
      expect((await backend.search("q", {})).ok).toBe(true);
    } finally {
      process.env[VAR] = before;
    }
  });
});

describe("an unset key without setup", () => {
  // The runtime fallback, the same in Python: a search sent without setup answers unavailable,
  // naming the variable, and sends nothing.
  for (const [name, make] of [
    ["exa", exa],
    ["tavily", tavily],
    ["brave", brave],
  ] as const) {
    test(`${name}: each search is unavailable, names the variable and sends nothing`, async () => {
      const t = scripted(Response.json({}));
      const got = await make(secret("THREADS_TEST_UNSET_KEY"), t).search(
        "q",
        {},
      );
      expect(got).toMatchObject({ ok: false, error: { code: "unavailable" } });
      expect(got.ok ? "" : got.error.message).toContain(
        "THREADS_TEST_UNSET_KEY",
      );
      expect(t.sent).toEqual([]);
    });
  }
});

describe("the live transport", () => {
  test("connects to the checked address, keeping the URL's host", async () => {
    const server = Bun.serve({
      port: 0,
      hostname: "127.0.0.1",
      fetch: async (req) =>
        new Response(
          `${req.method} ${req.headers.get("host")} ${await req.text()}`,
        ),
    });
    try {
      const url = `http://pinned.test:${server.port}/x`;
      const res = await liveTransport.fetch(url, "127.0.0.1", {
        method: "POST",
        headers: { "content-type": "text/plain" },
        body: "hi",
        signal: AbortSignal.timeout(5000),
      });
      expect(res.status).toBe(200);
      expect(await res.text()).toBe(`POST pinned.test:${server.port} hi`);
    } finally {
      server.stop(true);
    }
  });
});
