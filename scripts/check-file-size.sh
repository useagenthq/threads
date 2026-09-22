#!/usr/bin/env bash
# Fails if any hand-written source file is longer than MAX_LINES.
# Generated code is excluded. Keeps files small enough to read in one sitting.
set -euo pipefail

MAX_LINES=400
root="${1:-.}"

too_long=$(find "$root" -type f \( -name '*.ts' -o -name '*.py' \) \
  -not -path '*/node_modules/*' -not -path '*/.venv/*' \
  -not -path '*/generated/*' -not -path '*/dist/*' -print0 \
  | xargs -0 -r wc -l | awk -v max="$MAX_LINES" '$2 != "total" && $1 > max')

if [[ -n "$too_long" ]]; then
  echo "These files are over $MAX_LINES lines. Split them by responsibility:"
  echo "$too_long"
  exit 1
fi
echo "All source files are within $MAX_LINES lines."
