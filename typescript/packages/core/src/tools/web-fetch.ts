import { sha256Hex } from "../hash";
import type { ToolContext, ToolRun } from "../loop/types";
import { type Builtin, builtin, done } from "./builtin";
import { WebFetchInput } from "./gateway-inputs";
import { htmlTitle, htmlToMarkdown } from "./html";
import { liveTransport, vet, type WebTransport } from "./web-transport";

// web_fetch: host-side, read_only, fenced at the real transport. Every
// resolved address of every hop must be public (ssrf.ts); at most 5 same-host redirects, and a
// redirect to another host ends the call with its URL. The page is untrusted reference: the
// result records the final URL, status and content hash, and cites the fetched artifact.

const MAX_REDIRECTS = 5;
const MAX_BYTES = 5 << 20;
/** Text the model sees inline; the whole page stays readable with read_tool_result. */
const PART_CHARS = 16_384;
const TIMEOUT_MS = 30_000;
const REDIRECTS = new Set([301, 302, 303, 307, 308]);
const TEXTUAL =
  /^(text\/|application\/(json|xml|xhtml\+xml|[a-z.+-]+\+(json|xml))$)/;

async function body(res: Response): Promise<Uint8Array> {
  const reader = res.body?.getReader();
  if (reader === undefined) return new Uint8Array(0);
  const parts: Uint8Array[] = [];
  let total = 0;
  while (total < MAX_BYTES) {
    const { done: end, value } = await reader.read();
    if (end) break;
    parts.push(value);
    total += value.length;
  }
  await reader.cancel();
  const out = new Uint8Array(Math.min(total, MAX_BYTES));
  let at = 0;
  for (const part of parts) {
    out.set(part.subarray(0, out.length - at), at);
    at += Math.min(part.length, out.length - at);
  }
  return out;
}

type Fetched = { readonly url: URL; readonly res: Response } | ToolRun;

async function follow(
  start: URL,
  ctx: ToolContext,
  transport: WebTransport,
): Promise<Fetched> {
  let url = start;
  for (let hop = 0; ; hop += 1) {
    const target = await vet(url, transport);
    if ("denied" in target) return done(`denied: ${target.denied}`, true);
    // The lease is re-checked at the send point: a stale owner sends nothing.
    const fenced = await ctx.fence();
    if (!fenced.ok) return { kind: "not_sent" };
    const res = await transport.fetch(url.href, target.address, {
      signal: AbortSignal.any([ctx.signal, AbortSignal.timeout(TIMEOUT_MS)]),
      headers: {
        "user-agent": "threads-web-fetch/1",
        accept: "text/html, text/*;q=0.9, */*;q=0.1",
      },
    });
    const location = res.headers.get("location");
    if (!REDIRECTS.has(res.status) || location === null) return { url, res };
    await res.body?.cancel();
    const next = new URL(location, url);
    if (next.host !== url.host)
      return done(
        `redirected to ${next.href}, another host; call web_fetch with that URL to follow it`,
      );
    if (hop + 1 > MAX_REDIRECTS)
      return done(
        `more than ${MAX_REDIRECTS} redirects; stopped at ${next.href}`,
        true,
      );
    url = next;
  }
}

function record(
  url: URL,
  res: Response,
  bytes: Uint8Array,
  ctx: ToolContext,
  put: (bytes: Uint8Array) => string,
): ToolRun {
  const type =
    (res.headers.get("content-type") ?? "")
      .split(";")[0]
      ?.trim()
      .toLowerCase() ?? "";
  const media = /^[a-z0-9.+-]+\/[a-z0-9.+-]+$/.test(type)
    ? type
    : "application/octet-stream";
  const sha256 = sha256Hex(bytes);
  const head = `URL: ${url.href}\nStatus: ${res.status}\nSHA-256: ${sha256}\n`;
  if (type !== "" && !TEXTUAL.test(type))
    return done(
      `${head}unsupported content type ${type}; nothing to read`,
      true,
    );
  const raw = new TextDecoder().decode(bytes);
  const html = type === "text/html" || type === "application/xhtml+xml";
  const page = html ? htmlToMarkdown(raw) : raw;
  const title = html ? htmlTitle(raw) : undefined;
  const wrap = (text: string): string =>
    `${head}<reference source="web" url="${url.href}" untrusted="true">\n${text}\n</reference>`;
  const shown =
    page.length <= PART_CHARS
      ? page
      : `${page.slice(0, PART_CHARS)}\n[page truncated: read_tool_result(call_id="${ctx.callId}", offset, length) returns all of it]`;
  const ref = { sha256: put(bytes), bytes: bytes.length, media_type: media };
  return {
    kind: "done",
    output: wrap(page),
    isError: res.status >= 400,
    content: [
      { type: "text", text: wrap(shown) },
      {
        type: "citation",
        source_kind: "web",
        source_id: url.href,
        ref,
        ...(title === undefined ? {} : { title }),
      },
    ],
  };
}

export function webFetch(transport: WebTransport = liveTransport): Builtin {
  return builtin({
    name: "web_fetch",
    input: WebFetchInput,
    effect: "read_only",
    run: async ({ url }, ctx, env) => {
      let start: URL;
      try {
        start = new URL(url);
      } catch {
        return done(`not a URL: ${url}`, true);
      }
      const got = await follow(start, ctx, transport);
      if ("kind" in got) return got;
      const bytes = await body(got.res);
      return record(got.url, got.res, bytes, ctx, env.artifacts.put);
    },
  });
}
