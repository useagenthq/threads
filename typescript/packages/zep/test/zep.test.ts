import { describe, expect, test } from "bun:test";
import { secret } from "@threads/core";
import {
  dispatched,
  type Fetch,
  memoryProviderSuite,
} from "@threads/core/adapter";
import { z } from "zod";
import { zep } from "../src";

// zep() against an in-memory stand-in for the Zep Cloud graph API (no network): the shared
// provider suite and the fence at the SDK's transport hook.

type Episode = {
  uuid: string;
  content: string;
  metadata: unknown;
  graph: string;
};

const Body = z.object({
  graph_id: z.string(),
  query: z.string().default(""),
  data: z.string().optional(),
  metadata: z.unknown().optional(),
});
type Body = z.infer<typeof Body>;

function search(all: readonly Episode[], body: Body) {
  const words = body.query.toLowerCase().split(/\W+/).filter(Boolean);
  return all
    .filter((e) => e.graph === body.graph_id)
    .filter((e) => words.some((w) => e.content.toLowerCase().includes(w)))
    .map((e) => ({
      uuid: e.uuid,
      content: e.content,
      metadata: e.metadata,
      score: 0.8,
      created_at: "2026-01-01",
    }));
}

function backend(): { fetch: Fetch; requests: string[] } {
  const graphs = new Set<string>();
  const episodes = new Map<string, Episode>();
  const requests: string[] = [];
  const json = (body: unknown, status = 200) =>
    new Response(JSON.stringify(body), {
      status,
      headers: { "content-type": "application/json" },
    });
  const missing = () => json({ message: "not found" }, 404);
  const routes: Record<string, (body: Body) => Response> = {
    "POST graph/create": (body) => {
      graphs.add(body.graph_id);
      return json({ graph_id: body.graph_id });
    },
    "POST graph": (body) => {
      if (!graphs.has(body.graph_id)) return missing();
      const e = {
        uuid: `ep${episodes.size + 1}`,
        content: body.data ?? "",
        metadata: body.metadata,
        graph: body.graph_id,
      };
      episodes.set(e.uuid, e);
      return json({ ...e, created_at: "2026-01-01" });
    },
    "POST graph/search": (body) =>
      graphs.has(body.graph_id)
        ? json({ episodes: search([...episodes.values()], body) })
        : missing(),
  };
  const byId = (method: string, uuid: string): Response => {
    const e = episodes.get(uuid);
    if (e === undefined) return missing();
    if (method === "DELETE") episodes.delete(uuid);
    return json({ ...e, created_at: "2026-01-01" });
  };
  const fetch: Fetch = async (input, init) => {
    const request = new Request(input, init);
    const path = new URL(request.url).pathname.replace("/api/v2/", "");
    requests.push(`${request.method} ${path}`);
    const route = routes[`${request.method} ${path}`];
    if (route === undefined)
      return byId(request.method, path.split("/").at(-1) ?? "");
    return route(Body.parse(await request.json()));
  };
  return { fetch, requests };
}

process.env["THREADS_TEST_ZEP_KEY"] = "zep-test-key";
const apiKey = secret("THREADS_TEST_ZEP_KEY");
const baseUrl = "http://zep.test/api/v2";

memoryProviderSuite(
  "zep",
  async () => zep({ apiKey, baseUrl, fetch: backend().fetch }),
  {
    test: (name, body) => test(name, body),
  },
);

describe("zep transport", () => {
  test("a stale lease sends nothing: the fence is at the SDK's fetcher", async () => {
    const { fetch, requests } = backend();
    const p = zep({ apiKey, baseUrl, fetch });
    const stale = {
      fence: async () =>
        ({
          ok: false,
          error: { code: "stale_epoch", message: "lease lost" },
        }) as const,
    };
    const d = await dispatched(stale, () =>
      p.recall({ tenant_id: "t", agent: "a", scope: "s" }, "anything"),
    );
    expect({ sent: d.sent, refused: d.refused }).toEqual({
      sent: false,
      refused: true,
    });
    expect(requests).toEqual([]);
  });
});
