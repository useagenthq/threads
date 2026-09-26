#!/usr/bin/env bash
# Rebuilds the generated code from spec/ (it is not committed). Run after a clone, a pull, or a spec change.
#   scripts/generate.sh        both languages
#   scripts/generate.sh ts     TypeScript only (needs python3)
#   scripts/generate.sh py     Python only (needs uv)
set -euo pipefail
cd "$(dirname "$0")/.."
target="${1:-all}"

# The generators are 3.12 source. A bare `python3` is whatever the shell hands us, and on a
# stock macOS that is 3.9, which dies on `str | None` two hundred lines into gen_store_sql.py
# with a bare SyntaxError. Pick an interpreter that can actually run them, and say so plainly
# if there isn't one, rather than failing somewhere that reads like a bug in the spec tools.
runnable() { "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null; }

py=""
if [ -x python/.venv/bin/python ] && runnable python/.venv/bin/python; then
  py="$PWD/python/.venv/bin/python"
else
  for candidate in python3.13 python3.12 python3; do
    if command -v "$candidate" >/dev/null 2>&1 && runnable "$candidate"; then
      py="$candidate"
      break
    fi
  done
fi

if [ -z "$py" ]; then
  echo "scripts/generate.sh: needs Python 3.12 or newer to run the generators in spec/tools." >&2
  echo "  Found: $(python3 -V 2>&1 || echo 'no python3 on PATH')" >&2
  echo "  Fix it with either of:" >&2
  echo "    (cd python && uv sync)      # creates python/.venv, which this script prefers" >&2
  echo "    uv run --project python ./scripts/generate.sh" >&2
  exit 1
fi

mkdir -p python/src/threads/_generated typescript/packages/core/src/store/generated
touch python/src/threads/_generated/__init__.py
"$py" spec/tools/gen_store_sql.py
"$py" spec/tools/gen_model_catalogs.py
"$py" spec/tools/gen_unicode_fold.py
if [ "$target" != "py" ]; then
  "$py" spec/tools/gen_api_surface.py
fi
# Factory signature checks: TS files compiled by each package's tsc, a Python file for pyright.
"$py" spec/tools/gen_api_surface_factories.py
if [ "$target" != "ts" ]; then
  (cd python && uv run --quiet python tools/regen_models.py)
fi
