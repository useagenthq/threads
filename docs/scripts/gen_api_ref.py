"""Generate the API reference pages from spec/api.json and the bundled HTTP OpenAPI file.

Stdlib only and deterministic: the same spec always writes the same bytes.

    python3 docs/scripts/gen_api_ref.py          # write content/docs/reference/** and openapi.json
    python3 docs/scripts/gen_api_ref.py --check  # exit 1 if anything is out of date

The pages are Fumadocs MDX. The HTTP pages are generated from docs/openapi.json by
docs/scripts/gen-openapi.ts. The tables that keep unbuilt members out of the docs are in
api_ref/tables.py.
"""

import sys

# Works as a script (python3 docs/scripts/gen_api_ref.py) and as a module (-m docs.scripts...).
if __package__:
    from .api_ref.cli import main
else:
    from api_ref.cli import main

if __name__ == "__main__":
    sys.exit(main())
