// Checks every link in the content, the landing page and the repo README against the built site.
// Run after `next build`: a link must point at a prerendered page, and its #hash at an id on that page.
// Also checks that every meta.json page entry names a real file.
//
//   bun scripts/check-links.ts             internal links only (what CI runs)
//   bun scripts/check-links.ts --external  also fetch every http(s) link
//
// External links are opt-in: they depend on other people's servers, so a green main must not
// hinge on them. Run it by hand when touching docs, and read the failures rather than trusting them.
import { existsSync } from "node:fs";
import { glob, readFile } from "node:fs/promises";

const APP = ".next/server/app";
const SITE = "https://threadsai.dev";
// Routes that are not prerendered HTML pages but are valid link targets.
const OTHER_ROUTES = new Set(["/llms.txt", "/llms-full.txt", "/sitemap.xml"]);
// Stand-ins in example code. They name no real server, so fetching them proves nothing.
const PLACEHOLDER = /^https?:\/\/(localhost|127\.0\.0\.1|(\w+\.)*example\.(com|org))([:/]|$)/;

const ids = new Map<string, Set<string>>();
for await (const file of glob("**/*.html", { cwd: APP })) {
  const route = file === "index.html" ? "/" : `/${file.replace(/\.html$/, "")}`;
  const html = await readFile(`${APP}/${file}`, "utf8");
  ids.set(route, new Set([...html.matchAll(/\sid="([^"]+)"/g)].map((m) => m[1] ?? "")));
}

/** The route an MDX file renders as: route groups drop out, and index is the directory itself. */
function routeOf(file: string): string {
  const parts = file
    .replace(/^content\//, "")
    .replace(/\.mdx$/, "")
    .split("/")
    .filter((p) => !(p.startsWith("(") && p.endsWith(")")));
  if (parts.at(-1) === "index") parts.pop();
  return `/${parts.join("/")}`;
}

const broken: string[] = [];
const external = new Map<string, string>();
let checked = 0;

/** One link, resolved against the built site. `from` is the route a bare #hash belongs to. */
function check(where: string, link: string, from?: string): void {
  checked++;
  if (link.startsWith("http")) {
    if (!link.startsWith(SITE)) {
      if (!PLACEHOLDER.test(link) && !external.has(link)) external.set(link, where);
      return;
    }
    link = link.slice(SITE.length) || "/"; // our own site: resolve it locally, don't fetch it
  }
  const [path = "", hash] = link.split("#");
  const route = path === "" ? from : path.length > 1 ? path.replace(/\/$/, "") : path;
  if (route === undefined) return;
  if (OTHER_ROUTES.has(route)) return;
  const pageIds = ids.get(route);
  if (!pageIds) broken.push(`${where}: ${link} (no such page)`);
  else if (hash && !pageIds.has(hash)) broken.push(`${where}: ${link} (no #${hash} on the page)`);
}

// Markdown links and JSX href/url props, in that order. A link built from a template literal
// (`${githubUrl}/issues`) has no static target, so it is skipped rather than guessed at.
const LINK = /\]\(([^)\s]+)\)|(?:href|url)[=:]\s*\{?["'`]([^"'`]+)["'`]/g;
const links = (text: string): string[] =>
  [...text.matchAll(LINK)].map((m) => m[1] ?? m[2] ?? "").filter((l) => l !== "" && !l.includes("${"));

// The docs site: MDX pages carry their own route, so a bare #hash resolves against themselves.
for await (const file of glob("content/**/*.mdx")) {
  const from = routeOf(file);
  for (const link of links(await readFile(file, "utf8"))) check(file, link, from);
}
for (const pattern of ["app/**/*.tsx", "components/**/*.tsx"]) {
  for await (const file of glob(pattern)) {
    for (const link of links(await readFile(file, "utf8"))) check(file, link);
  }
}

// The repo README: its threadsai.dev links are this site, and its relative links are repo files.
const readme = await readFile("../README.md", "utf8");
for (const link of links(readme)) {
  if (link.startsWith("http") || link.startsWith("#")) check("README.md", link);
  else {
    checked++;
    const [path = ""] = link.split("#");
    if (!existsSync(`../${path}`)) broken.push(`README.md: ${link} (no such file)`);
  }
}

// Every meta.json entry names a page. A separator ("---[Icon]Title---") names none.
for await (const file of glob("content/**/meta.json")) {
  const dir = file.slice(0, file.lastIndexOf("/"));
  const meta: unknown = JSON.parse(await readFile(file, "utf8"));
  const pages = (meta as { pages?: unknown }).pages;
  if (!Array.isArray(pages)) continue;
  for (const page of pages) {
    if (typeof page !== "string" || page.startsWith("---")) continue;
    checked++;
    if (!existsSync(`${dir}/${page}.mdx`) && !existsSync(`${dir}/${page}`))
      broken.push(`${file}: "${page}" (no such page)`);
  }
}

if (process.argv.includes("--external")) {
  const results = await Promise.all(
    [...external].map(async ([url, where]) => {
      try {
        const res = await fetch(url, { redirect: "follow", signal: AbortSignal.timeout(30_000) });
        // 401/403 means the URL is there and wants credentials, which is the documented behaviour
        // of the endpoints we link to; only a missing or broken page is a dead link.
        const ok = res.status < 400 || res.status === 401 || res.status === 403;
        return ok ? null : `${where}: ${url} (HTTP ${res.status})`;
      } catch (error) {
        return `${where}: ${url} (${error instanceof Error ? error.message : String(error)})`;
      }
    }),
  );
  broken.push(...results.filter((r) => r !== null));
  checked += external.size;
}

for (const b of broken) console.log(b);
const note = process.argv.includes("--external") ? "" : ` (${external.size} external skipped)`;
console.log(`${checked} links checked, ${broken.length} broken${note}`);
process.exit(broken.length > 0 ? 1 : 0);
