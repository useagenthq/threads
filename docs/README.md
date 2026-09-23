# threads docs

The website for Threads AI: the landing page at `/` and the documentation at `/docs`. One Next.js app built with [Fumadocs](https://fumadocs.dev). It has its own `package.json` and is not part of the `typescript/` workspace.

## Run it

Requires [Bun](https://bun.sh) and Python 3.12 or newer (for the reference generator).

```sh
cd docs
bun install
bun run dev           # http://localhost:3000
```

Production build (a static site in `out/`; see [Deploy](#deploy) to serve it):

```sh
bun run build
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
| `app/api/search/` | Search index (Orama, built at build time and searched in the browser) |
| `content/docs/(guides)/` | The guides. `meta.json` holds the sidebar order and sections |
| `content/docs/reference/` | Generated API reference. Do not edit by hand |
| `content/docs/http-api/` | Generated HTTP API reference. Do not edit by hand |
| `openapi.json` | Generated: the host OpenAPI file with its `$ref`s bundled |
| `components/` | Landing sections, the logo, the `Field` component and the MDX component map |
| `scripts/` | Generators and the link checker |
| `public/logo/`, `app/icon.svg` | Brand assets |

The old Mintlify URLs (`/quickstart`, `/agents/tools`, `/reference/...`) redirect to their `/docs/...` pages (`public/_redirects`).

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

The site is a static export hosted on Cloudflare Pages (project `threadsai`). `bun run build` writes everything to `out/`: the HTML pages, the search index (`/api/search`, searched in the browser), llms.txt, the sitemap, robots.txt, the OG images and each page's Markdown. Three files cover what a static export can't do by itself:

- `public/_redirects`: the old Mintlify URLs, as Cloudflare Pages redirect rules.
- `public/_headers`: content types for the Markdown files and for the files without an extension.
- `functions/_middleware.ts`: a Pages Function that serves a page's Markdown at `<url>.md`, or at `<url>` when the `Accept` header prefers Markdown. `public/_routes.json` runs it on `/docs` requests only; everything else is served as static files.

`bun run dev` has neither the redirects nor the Markdown routes. To try the site the way Cloudflare serves it:

```sh
bun run build
bunx wrangler pages dev out     # http://localhost:8788
```

### Deploys

Every push to `main` that touches `docs/` or `spec/` builds and deploys through `.github/workflows/docs-deploy.yml` (it can also be run by hand from the Actions tab). It needs two repository secrets:

- `CLOUDFLARE_ACCOUNT_ID`: the account ID, shown by `bunx wrangler whoami` or in the dashboard.
- `CLOUDFLARE_API_TOKEN`: an API token with the **Cloudflare Pages: Edit** permission.

To deploy from your machine, run this in `docs/` (wrangler looks for `functions/` in the current directory):

```sh
bun run build
bunx wrangler pages deploy out --project-name threadsai --branch main
```

### Custom domain

In the Cloudflare dashboard, open **Workers & Pages > threadsai > Custom domains**, choose **Set up a custom domain** and enter `threadsai.dev`. With the `threadsai.dev` zone on the same Cloudflare account, the DNS record is created for you; with DNS elsewhere, add the `CNAME` to `threadsai.pages.dev` that the dashboard shows. Add `www.threadsai.dev` the same way if you want it, and redirect it to `threadsai.dev` with a redirect rule on the zone.

`lib/shared.ts` holds the site URL (`https://threadsai.dev`) used for the sitemap, canonical URLs and OG images. Change it there if the domain changes.
