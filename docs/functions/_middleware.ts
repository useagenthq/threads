// Cloudflare Pages Function: serves a docs page as Markdown at `<url>.md`, or at `<url>`
// when the request's Accept header prefers Markdown. public/_routes.json limits it to /docs.
import { isMarkdownPreferred, rewritePath } from "fumadocs-core/negotiation";
import { docsContentRoute, docsRoute } from "../lib/shared";

type Context = {
  readonly request: Request;
  readonly next: () => Promise<Response>;
  readonly env: { readonly ASSETS: { fetch: (url: URL) => Promise<Response> } };
};

const { rewrite: rewriteDocs } = rewritePath(
  `${docsRoute}{/*path}`,
  `${docsContentRoute}{/*path}/content.md`,
);
const { rewrite: rewriteSuffix } = rewritePath(
  `${docsRoute}{/*path}.md`,
  `${docsContentRoute}{/*path}/content.md`,
);

export async function onRequest({ request, next, env }: Context): Promise<Response> {
  const { pathname } = new URL(request.url);
  const suffixed = rewriteSuffix(pathname);
  if (suffixed) return env.ASSETS.fetch(new URL(suffixed, request.url));

  if (!isMarkdownPreferred(request)) return next();
  const negotiated = rewriteDocs(pathname);
  if (!negotiated) return next();

  const asset = await env.ASSETS.fetch(new URL(negotiated, request.url));
  const response = new Response(asset.body, asset);
  // this URL has two representations, selected by `Accept`
  response.headers.set("Vary", "Accept");
  return response;
}
