# threads docs

The website for Threads AI: the landing page at `/` and the documentation at `/docs`. One Next.js app built with [Fumadocs](https://fumadocs.dev). It has its own `package.json` and is not part of the `typescript/` workspace.

## Run it

Requires [Bun](https://bun.sh) and Python 3.12 or newer (for the reference generator).

```sh
cd docs
bun install
bun run dev           # http://localhost:3000
```

Production build:

```sh
bun run build
bun run start
```

## Before you push

```sh
bun run check:gen     # the generated reference matches spec/
bun run lint          # Biome
bun run types:check   # tsc
bun run build         # every page compiles and prerenders
bun run check:links   # after a build: every internal link and #anchor resolves
```

## Layout

| Path | What it is |
|---|---|
| `app/(home)/` | The landing page and its layout (nav and footer) |
| `app/docs/` | The docs layout and page renderer |
| `app/llms.txt`, `app/llms-full.txt`, `app/llms.mdx/` | Plain-text docs for LLMs. Any docs page is also served as Markdown at `<url>.md` |
| `app/og/`, `app/opengraph-image.tsx` | Open Graph images |
| `app/sitemap.ts`, `app/robots.ts` | Sitemap and robots.txt |
| `app/api/search/` | Search (Orama, built from the pages at build time) |
| `content/docs/(guides)/` | The guides. `meta.json` holds the sidebar order and sections |
| `content/docs/reference/` | Generated API reference. Do not edit by hand |
| `content/docs/http-api/` | Generated HTTP API reference. Do not edit by hand |
| `openapi.json` | Generated: the host OpenAPI file with its `$ref`s bundled |
| `components/` | Landing sections, the logo, the `Field` component and the MDX component map |
| `scripts/` | Generators and the link checker |
| `public/logo/`, `app/icon.svg` | Brand assets |

The old Mintlify URLs (`/quickstart`, `/agents/tools`, `/reference/...`) redirect to their `/docs/...` pages (`next.config.mjs`).

## The generated reference

Two generators, both deterministic, both with a `--check` mode for CI:

- `scripts/gen_api_ref.py` (stdlib Python) writes `content/docs/reference/` from `spec/api.json`, and `openapi.json` from `spec/schema/host-api/openapi.json`.
- `scripts/gen-openapi.ts` writes `content/docs/http-api/` from `openapi.json` with `fumadocs-openapi`.

Regenerate both after changing either spec file:

```sh
bun run gen           # from docs/
bun run check:gen     # exits 1 if anything is out of date
```

The contract also lists members that aren't built yet. `gen_api_ref.py` keeps them out with the `NOT_BUILT` table in `scripts/api_ref/tables.py` (and `NOT_BUILT_ROUTES` for HTTP routes) and marks one-language members with `ONLY_IN`. Update the tables when a member lands.

## Writing guides

- Document only what exists in the code today. Say "TypeScript only" or "Python only" with a `<Callout>` where a feature exists in one language.
- Put TypeScript and Python side by side in synced tabs. `groupId="lang"` and `persist` make the reader's choice stick across pages and visits:

  ````mdx
  <Tabs items={["TypeScript", "Python"]} groupId="lang" persist>
  <Tab value="TypeScript">

  ```ts
  ...
  ```

  </Tab>
  <Tab value="Python">

  ```python
  ...
  ```

  </Tab>
  </Tabs>
  ````

- Check every example against the code before merging: type-check TypeScript with the repo's tsconfig, and run or construct Python with `uv run python` and `uv run pyright`.
- Give every page a `title`, a one-sentence `description` and an `icon`: a [lucide](https://lucide.dev/icons) name in PascalCase (`Rocket`, `FlaskConical`). Card icons are imported from `lucide-react` at the top of the page.
- Add a new page to `content/docs/(guides)/meta.json`, or it won't appear in the sidebar.
- Components available in every page: `Callout` (`type="warn"`, `"idea"`), `Cards`/`Card`, `Steps`/`Step` (a `###` heading inside each step is its title), `Tabs`/`Tab`, `Accordions`/`Accordion`, and `Field` (one parameter: `name`, `type`, `required`, `default`, prose as children).

## Deploy

The site is a standard Next.js app. The search index, llms.txt and OG images are built at build time; search and the `.md` negotiation run on the server, so deploy it as a Next.js app, not a static export.

### Vercel

1. Import the repository and set **Root Directory** to `docs`. Vercel detects Next.js and Bun (from `bun.lock`).
2. Build command `bun run build`, install command `bun install` (the defaults once Bun is detected).
3. Under **Domains**, add `threadsai.dev` (and `www.threadsai.dev` redirecting to it). At your DNS provider, point the apex `A` record to `76.76.21.21` and `www` to `cname.vercel-dns.com`, or use the values Vercel shows.

### Cloudflare

Use the [OpenNext Cloudflare adapter](https://opennext.js.org/cloudflare), which runs Next.js on Workers:

1. `bun add -d @opennextjs/cloudflare wrangler` in `docs/`, and add a `wrangler.jsonc` as the adapter's guide describes.
2. Build and deploy with `bunx opennextjs-cloudflare build && bunx opennextjs-cloudflare deploy`, or connect the repository in Workers Builds with root `docs`.
3. Add `threadsai.dev` as a **Custom Domain** on the Worker. With the zone on Cloudflare, the DNS record is created for you.

`lib/shared.ts` holds the site URL (`https://threadsai.dev`) used for the sitemap, canonical URLs and OG images. Change it there if the domain changes.
