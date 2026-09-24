#!/usr/bin/env bash
# Rebuilds the generated code from spec/ (it is not committed). Run after a clone, a pull, or a spec change.
#   scripts/generate.sh        both languages
#   scripts/generate.sh ts     TypeScript only (needs python3)
#   scripts/generate.sh py     Python only (needs uv)
set -euo pipefail
cd "$(dirname "$0")/.."
target="${1:-all}"
mkdir -p python/src/threads/_generated typescript/packages/core/src/store/generated
touch python/src/threads/_generated/__init__.py
python3 spec/tools/gen_store_sql.py
python3 spec/tools/gen_model_catalogs.py
if [ "$target" != "py" ]; then
  python3 spec/tools/gen_api_surface.py
fi
# Factory signature checks: TS files compiled by each package's tsc, a Python file for pyright.
python3 spec/tools/gen_api_surface_factories.py
if [ "$target" != "ts" ]; then
  (cd python && uv run --quiet python tools/regen_models.py)
fi
