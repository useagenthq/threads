#!/usr/bin/env bash
# Prints the base commit the API surface gate compares against: its SHA and a newline on stdout,
# nothing else. Every message goes to stderr. Exits 1 when no base resolves, so the gate never
# silently skips.
#   scripts/surface-base.sh --event pull_request --base-ref main   # merge base with origin/main
#   scripts/surface-base.sh --event push --before <sha>            # the pushed-over commit
# An empty --base-ref or --before is ignored; an all-zero --before (a new branch) falls back to
# the merge base with origin/main, as does a --before that no longer exists (a force-push leaves
# github.event.before pointing at a commit that has been dropped).
set -euo pipefail

event="" base_ref="" before=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --event) event="${2-}"; shift 2 ;;
    --base-ref) base_ref="${2-}"; shift 2 ;;
    --before) before="${2-}"; shift 2 ;;
    *) echo "surface-base.sh: unknown argument $1" >&2; exit 2 ;;
  esac
done

sha=""
if [[ "$event" == "pull_request" && -n "$base_ref" ]]; then
  git fetch --no-tags --quiet origin "$base_ref" >&2 || true
  sha=$(git merge-base HEAD "origin/$base_ref" 2>/dev/null || true)
elif [[ "$event" == "push" && -n "$before" && ! "$before" =~ ^0+$ ]]; then
  if git cat-file -e "$before^{commit}" 2>/dev/null; then
    sha="$before"
  else
    # A force-push leaves github.event.before pointing at a dropped commit. The merge base with
    # main is the right answer then, unless it is HEAD itself — which is what a shallow clone
    # yields, and comparing a commit with itself would pass the gate without looking.
    sha=$(git merge-base HEAD origin/main 2>/dev/null || true)
    [[ -n "$sha" && "$sha" == "$(git rev-parse HEAD 2>/dev/null)" ]] && sha=""
  fi
else
  sha=$(git merge-base HEAD origin/main 2>/dev/null || true)
fi

if [[ ! "$sha" =~ ^[0-9a-f]{40}$ ]] || ! git cat-file -e "$sha^{commit}" 2>/dev/null; then
  echo "surface gate: can't resolve the base commit (event $event, base ref $base_ref, before $before); fetch history or check the workflow checkout" >&2
  exit 1
fi
echo "$sha"
