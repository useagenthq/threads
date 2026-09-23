# threads docs

The documentation site, built with [Mintlify](https://mintlify.com).

## Preview locally

Requires Node.js 20 or newer.

```sh
cd docs
npx mint dev          # http://localhost:3000
```

Before you push a change:

```sh
npx mint validate     # docs.json and every page parse
npx mint broken-links # every internal link resolves
```

## Layout

| Path | What it is |
|---|---|
| `docs.json` | Site config: theme, colors, navigation |
| `index.mdx` | The landing page (custom layout) |
| `*.mdx` and folders | Guides, grouped as in the Guides tab |
| `reference/` | Generated. Do not edit by hand |
| `scripts/gen_api_ref.py` | Generates `reference/` and the API Reference and HTTP API tabs |
| `logo/`, `favicon.svg` | Brand assets |

## The generated reference

`reference/` is built from `spec/api.json` (the public API contract) and `spec/schema/host-api/openapi.json` (the host HTTP API). Regenerate it after changing either:

```sh
python3 docs/scripts/gen_api_ref.py          # from the repo root
python3 docs/scripts/gen_api_ref.py --check  # exits 1 if reference/ is out of date
```

The contract also lists members that aren't built yet. The script keeps them out with its `NOT_BUILT` table and marks one-language members with `ONLY_IN`. Update both tables when a member lands.

## Writing guides

- Document only what exists in the code today. Say "TypeScript only" or "Python only" with a `<Note>` where a feature exists in one language.
- Put TypeScript and Python side by side in a `<CodeGroup>` with titled fences: ` ```ts TypeScript ` and ` ```python Python `.
- Check every example against the code before merging: type-check TypeScript with the repo's tsconfig, and run or construct Python with `uv run python` and `uv run pyright`.
- Give every page a `title`, a one-sentence `description` and an `icon` (lucide names).
