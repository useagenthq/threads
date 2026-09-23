import { createMDX } from "fumadocs-mdx/next";

const withMDX = createMDX();

// Pages that lived at the site root on Mintlify now live under /docs.
const SECTIONS = "agents|multi-agent|sandboxes|host|memory|control|production|evals|reference";
const PAGES = "quickstart|installation|how-it-works";

/** @type {import('next').NextConfig} */
const config = {
  reactStrictMode: true,
  async redirects() {
    return [
      { source: "/introduction", destination: "/docs", permanent: true },
      { source: `/:page(${PAGES})`, destination: "/docs/:page", permanent: true },
      { source: `/:section(${SECTIONS})/:path*`, destination: "/docs/:section/:path*", permanent: true },
    ];
  },
};

export default withMDX(config);
