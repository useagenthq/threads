import { z } from "zod";
import { credential, type Secret } from "../agent/secret";
import { err, ok } from "../result";
import type { SearchBackend, SearchHit } from "./web-search";
import {
  liveTransport,
  type Sent,
  vet,
  type WebTransport,
} from "./web-transport";

// web_search backends (spec/api.json SearchBackend): exa(), brave() and
// tavily(). Each takes its API key as a secret(), revealed on the host when a search is sent,
// and sends one JSON request through the host web transport. No SDK.

const MAX_RESULTS = 10;
const TIMEOUT_MS = 30_000;

// Provider answers gain fields over time: unknown ones are ignored, known ones checked.
const Hit = z.object({
  url: z.string(),
  title: z.string().nullish(),
  text: z.string().nullish(),
  description: z.string().nullish(),
  content: z.string().nullish(),
});
const Results = z.object({ results: z.array(Hit).default([]) });
const Brave = z.object({ web: Results.default({ results: [] }) });
type Hit = z.infer<typeof Hit>;

type Built = { readonly url: string; readonly init: Omit<Sent, "signal"> };
type Build = (
  query: string,
  key: string,
  allowed: readonly string[],
  blocked: readonly string[],
) => Built;

const json = (
  headers: Record<string, string>,
  body: unknown,
): Omit<Sent, "signal"> => ({
  method: "POST",
  headers: { ...headers, "content-type": "application/json" },
  body: JSON.stringify(body),
});

const hitOf = (h: Hit): SearchHit => ({
  url: h.url,
  title: h.title ?? h.url,
  snippet: (h.text ?? h.description ?? h.content ?? "").trim().slice(0, 500),
});

function backend(
  factory: string,
  apiKey: Secret,
  build: Build,
  parse: (raw: unknown) => readonly Hit[] | undefined,
  transport: WebTransport,
): SearchBackend {
  const key = credential(factory, "apiKey", apiKey, apiKey.name);
  return {
    // An unset key fails setup (missing_secret), so check() reports it before any run.
    setup: async () => {
      key();
    },
    search: async (query, options) => {
      let resolved: string;
      try {
        // Kept from setup; only a backend used without setup reads the env here.
        resolved = key();
      } catch (error) {
        return err({ code: "unavailable", message: String(error) });
      }
      const { url, init } = build(
        query,
        resolved,
        options.allowedDomains ?? [],
        options.blockedDomains ?? [],
      );
      const target = await vet(new URL(url), transport);
      if ("denied" in target)
        return err({ code: "unavailable", message: target.denied });
      const signal = AbortSignal.any([
        ...(options.signal === undefined ? [] : [options.signal]),
        AbortSignal.timeout(TIMEOUT_MS),
      ]);
      const res = await transport.fetch(url, target.address, {
        ...init,
        signal,
      });
      if (res.status !== 200)
        return err({
          code: "unavailable",
          message: `search answered ${res.status}`,
        });
      let raw: unknown;
      try {
        raw = await res.json();
      } catch {
        return err({
          code: "unavailable",
          message: "unreadable search response",
        });
      }
      const hits = parse(raw);
      return hits === undefined
        ? err({ code: "unavailable", message: "unreadable search response" })
        : ok(hits.slice(0, MAX_RESULTS).map(hitOf));
    },
  };
}

const results = (raw: unknown): readonly Hit[] | undefined =>
  Results.safeParse(raw).data?.results;

export function exa(
  apiKey: Secret,
  transport: WebTransport = liveTransport,
): SearchBackend {
  return backend(
    "exa",
    apiKey,
    (query, key, allow, block) => ({
      url: "https://api.exa.ai/search",
      init: json(
        { "x-api-key": key },
        {
          query,
          numResults: MAX_RESULTS,
          contents: { text: true },
          ...(allow.length === 0 ? {} : { includeDomains: allow }),
          ...(block.length === 0 ? {} : { excludeDomains: block }),
        },
      ),
    }),
    results,
    transport,
  );
}

export function tavily(
  apiKey: Secret,
  transport: WebTransport = liveTransport,
): SearchBackend {
  return backend(
    "tavily",
    apiKey,
    (query, key, allow, block) => ({
      url: "https://api.tavily.com/search",
      init: json(
        { authorization: `Bearer ${key}` },
        {
          query,
          max_results: MAX_RESULTS,
          ...(allow.length === 0 ? {} : { include_domains: allow }),
          ...(block.length === 0 ? {} : { exclude_domains: block }),
        },
      ),
    }),
    results,
    transport,
  );
}

export function brave(
  apiKey: Secret,
  transport: WebTransport = liveTransport,
): SearchBackend {
  return backend(
    "brave",
    apiKey,
    (query, key, allow, block) => {
      // Brave has no domain parameters: its query operators carry them.
      const terms = [
        query,
        ...allow.slice(0, 1).map((d) => `site:${d}`),
        ...block.map((d) => `-site:${d}`),
      ];
      const q = new URLSearchParams({
        q: terms.join(" "),
        count: String(MAX_RESULTS),
      });
      return {
        url: `https://api.search.brave.com/res/v1/web/search?${q}`,
        init: {
          headers: { accept: "application/json", "x-subscription-token": key },
        },
      };
    },
    (raw) => Brave.safeParse(raw).data?.web.results,
    transport,
  );
}
