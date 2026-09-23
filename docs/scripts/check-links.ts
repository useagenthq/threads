// Checks every internal link in the content and the landing page against the built site.
// Run after `next build`: a link must point at a prerendered page, and its #hash at an id on that page.
//
//   bun scripts/check-links.ts
import { glob, readFile } from "node:fs/promises";

const APP = ".next/server/app";
// Routes that are not prerendered HTML pages but are valid link targets.
const OTHER_ROUTES = new Set(["/llms.txt", "/llms-full.txt", "/sitemap.xml"]);

const ids = new Map<string, Set<string>>();
for await (const file of glob("**/*.html", { cwd: APP })) {
  const route = file === "index.html" ? "/" : `/${file.replace(/\.html$/, "")}`;
  const html = await readFile(`${APP}/${file}`, "utf8");
  ids.set(route, new Set([...html.matchAll(/\sid="([^"]+)"/g)].map((m) => m[1] ?? "")));
}

const LINK = /\]\((\/[^)\s]*)\)|href[=:]\s*\{?["'`](\/[^"'`]*)["'`]/g;
const sources = ["content/**/*.mdx", "app/**/*.tsx", "components/**/*.tsx"];
const broken: string[] = [];
let checked = 0;

for (const pattern of sources) {
  for await (const file of glob(pattern)) {
    const text = await readFile(file, "utf8");
    for (const m of text.matchAll(LINK)) {
      const link = m[1] ?? m[2] ?? "";
      const [path = "", hash] = link.split("#");
      const route = path.length > 1 ? path.replace(/\/$/, "") : path;
      checked++;
      if (OTHER_ROUTES.has(route)) continue;
      const pageIds = ids.get(route);
      if (!pageIds) broken.push(`${file}: ${link} (no such page)`);
      else if (hash && !pageIds.has(hash)) broken.push(`${file}: ${link} (no #${hash} on the page)`);
    }
  }
}

for (const b of broken) console.log(b);
console.log(`${checked} internal links checked, ${broken.length} broken`);
process.exit(broken.length > 0 ? 1 : 0);
