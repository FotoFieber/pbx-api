#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Block installation-specific data and secrets from entering the repository.
#
#   leak-check.sh --staged   check lines added in the index (pre-commit)
#   leak-check.sh --tree REV check every tracked file at REV (pre-push)
#
# A personal denylist (one extended regex per line, '#' comments) is read from
# $PBX_API_DENYLIST or ~/.config/pbx-api/denylist. It lives outside the repository
# on purpose: it contains exactly the data that must not be published.
set -euo pipefail

mode=${1:---staged}
rev=${2:-HEAD}
denylist=${PBX_API_DENYLIST:-$HOME/.config/pbx-api/denylist}
fail=0

# generic secret patterns (kept small to avoid noise)
generic='-----BEGIN [A-Z ]*PRIVATE KEY-----|(api|secret|token|passwd|password)[_-]?(key)?["'"'"']?[[:space:]]*[:=][[:space:]]*["'"'"'][A-Za-z0-9+/_-]{16,}["'"'"']'

content() {
  if [ "$mode" = "--staged" ]; then
    git diff --cached -U0 --no-color --diff-filter=ACMR | grep -E '^\+' | grep -vE '^\+\+\+ ' || true
  else
    git grep -I -h -e '' "$rev" -- . || true
  fi
}

text=$(content)

if [ -f "$denylist" ]; then
  patterns=$(grep -vE '^\s*(#|$)' "$denylist" || true)
  if [ -n "$patterns" ]; then
    if hits=$(printf '%s\n' "$text" | grep -nE -f <(printf '%s\n' "$patterns")); then
      echo "leak-check: installation-specific data found (denylist $denylist):" >&2
      printf '%s\n' "$hits" | head -20 >&2
      fail=1
    fi
  fi
else
  echo "leak-check: no denylist at $denylist (only generic checks)" >&2
fi

if hits=$(printf '%s\n' "$text" | grep -nEi -e "$generic"); then
  echo "leak-check: possible secret:" >&2
  printf '%s\n' "$hits" | head -20 >&2
  fail=1
fi

if command -v gitleaks >/dev/null 2>&1; then
  # gitleaks 8.19+ has "git"/"dir"; older releases (e.g. Ubuntu 24.04: 8.16) only
  # "protect"/"detect"
  if gitleaks dir --help >/dev/null 2>&1; then
    staged_scan=(gitleaks git --pre-commit --staged)
    tree_scan=(gitleaks dir)
  else
    staged_scan=(gitleaks protect --staged)
    tree_scan=(gitleaks detect --no-git --source)
  fi
  if [ "$mode" = "--staged" ]; then
    "${staged_scan[@]}" --redact --no-banner >/dev/null || fail=1
  else
    # scan the tracked files at REV, not the working directory (which may differ
    # and contains untracked files such as .venv)
    snapshot=$(mktemp -d)
    trap 'rm -rf "$snapshot"' EXIT
    git archive "$rev" | tar -x -C "$snapshot"
    "${tree_scan[@]}" "$snapshot" --redact --no-banner >/dev/null || fail=1
  fi
else
  echo "leak-check: gitleaks not installed, skipped (recommended: https://github.com/gitleaks/gitleaks)" >&2
fi

exit $fail
