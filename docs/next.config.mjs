import { createMDX } from "fumadocs-mdx/next";

const withMDX = createMDX();

// A static export: `next build` writes the whole site to out/ for Cloudflare Pages.
// An export has no server, so redirects live in public/_redirects and the Markdown
// negotiation that proxy.ts did lives in functions/docs/_middleware.ts.
/** @type {import('next').NextConfig} */
const config = {
  output: "export",
  reactStrictMode: true,
};

export default withMDX(config);
